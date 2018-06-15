import json
import logging
import os
from copy import deepcopy
from functools import partial

from connexion.utils import boolean
from jsonschema import RefResolver, draft4_format_checker, ValidationError
from jsonschema.validators import Draft4Validator
from six import string_types
from swagger_spec_validator import ref_validators
from swagger_spec_validator.validator20 import deref

from walkoff.definitions import ApiData, DeviceApi
from walkoff.helpers import get_function_arg_names, format_exception_message
from walkoff.appgateway.apiutil import InvalidArgument, InvalidApi

logger = logging.getLogger(__name__)

TYPE_MAP = {
    'integer': int,
    'number': float,
    'boolean': boolean,
    'string': str,
    'user': int,
    'role': int
}

reserved_return_codes = ['UnhandledException', 'InvalidInput', 'EventTimedOut']


def make_type(value, type_literal):
    type_func = TYPE_MAP.get(type_literal)
    if (isinstance(value, dict) or isinstance(value, list)) and type_func == str:
        return json.dumps(value)
    else:
        return type_func(value)


def convert_primitive_type(value, parameter_type):
    return make_type(value, parameter_type)


def convert_primitive_array(values, parameter_type):
    return [convert_primitive_type(value, parameter_type) for value in values]


def convert_array(schema, param_in, message_prefix):
    if isinstance(schema, ApiData):
        schema = schema.__dict__
    if 'items' not in schema:
        return param_in
    item_type = schema['items']['type']
    if item_type in TYPE_MAP:
        try:
            return convert_primitive_array(param_in, item_type)
        except ValueError:
            items = str(param_in)
            items = items if len(items) < 30 else '{0}...]'.format(items[:30])
            message = '{0} has invalid input. Input {1} could not be converted to array ' \
                      'with type "object"'.format(message_prefix, items)
            logger.error(message)
            raise InvalidArgument(message)
    else:
        return [convert_json(schema['items'], param, message_prefix) for param in param_in]


def __convert_json(schema, param_in, message_prefix):
    if isinstance(param_in, string_types):
        try:
            param_in = json.loads(param_in)
        except (ValueError, TypeError):
            raise InvalidArgument(
                '{0} A JSON object was expected. '
                'Instead got "{1}" of type {2}.'.format(
                    message_prefix,
                    param_in,
                    type(param_in).__name__))
    if not isinstance(param_in, dict):
        raise InvalidArgument(
            '{0} A JSON object was expected. '
            'Instead got "{1}" of type {2}.'.format(
                message_prefix,
                param_in,
                type(param_in).__name__))
    if 'properties' not in schema:
        return param_in
    ret = {}
    for param_name, param_value in param_in.items():
        if param_name in schema['properties']:
            ret[param_name] = convert_json(schema['properties'][param_name], param_value, message_prefix)
        else:
            raise InvalidArgument('{0} Input has unknown parameter {1}'.format(message_prefix, param_name))
    return ret


def convert_json(spec, param_in, message_prefix):
    if isinstance(spec, ApiData):
        spec = spec.__dict__
    if 'type' in spec:
        parameter_type = spec['type']
        if parameter_type in TYPE_MAP:
            try:
                return convert_primitive_type(param_in, parameter_type)
            except ValueError:
                message = (
                    '{0} has invalid input. '
                    'Input {1} could not be converted to type {2}'.format(message_prefix, param_in, parameter_type))
                logger.error(message)
                raise InvalidArgument(message)
        elif parameter_type == 'array':
            return convert_array(spec, param_in, message_prefix)
        elif parameter_type == 'object':
            return __convert_json(spec, param_in, message_prefix)
        else:
            raise InvalidApi('{0} has invalid api'.format(message_prefix))
    elif 'schema' in spec:
        return convert_json(spec['schema'], param_in, message_prefix)
    else:
        raise InvalidApi('{0} has invalid api'.format(message_prefix))


def validate_app_spec(spec, app_name, walkoff_schema_path, spec_url='', http_handlers=None):
    from walkoff.appgateway import get_all_transforms_for_app
    from walkoff.appgateway import get_all_conditions_for_app
    walkoff_resolver = validate_spec_json(
        spec,
        os.path.join(walkoff_schema_path),
        spec_url,
        http_handlers)
    dereference = partial(deref, resolver=walkoff_resolver)
    dereferenced_spec = dereference(spec)
    if 'actions' in dereferenced_spec:
        actions = dereference(dereferenced_spec['actions'])
        validate_actions(actions, dereference, app_name)
    if 'conditions' in dereferenced_spec:
        condition_spec = dereference(dereferenced_spec['conditions'])
        validate_condition_transform_params(condition_spec, app_name, 'Condition', get_all_conditions_for_app(app_name),
                                            dereference)
    if 'transforms' in dereferenced_spec:
        transform_spec = dereference(dereferenced_spec['transforms'])
        validate_condition_transform_params(transform_spec, app_name, 'Transform', get_all_transforms_for_app(app_name),
                                            dereference)
    definitions = dereference(dereferenced_spec.get('definitions', {}))
    devices = dereference(dereferenced_spec.get('devices', {}))
    validate_definitions(definitions, dereference)
    validate_devices_api({key: DeviceApi(value) for key, value in devices.items()}, app_name)


def validate_data_in_param(params, data_in_param_name, message_prefix):
    data_in_param = next((param for param in params if param['name'] == data_in_param_name), None)
    if data_in_param is None:
        raise InvalidApi(
            '{0} has a data_in param {1} '
            'for which it does not have a '
            'corresponding parameter'.format(message_prefix, data_in_param_name))
    elif not data_in_param.get('required', False):
        raise InvalidApi(
            '{0} has a data_in param {1} which is not marked as required in the api. '
            'Add "required: true" to parameter specification for {1}'.format(message_prefix,
                                                                             data_in_param_name))


def validate_condition_transform_params(spec, app_name, action_type, defined_actions, dereferencer):
    from walkoff.appgateway import get_transform
    from walkoff.appgateway import get_condition
    seen = set()
    for action_name, action in spec.items():
        action = dereferencer(action)
        action_params = dereferencer(action.get('parameters', []))
        if action['run'] not in defined_actions:
            raise InvalidApi('{0} action {1} has a "run" param {2} '
                             'which is not defined'.format(action_type, action_name, action['run']))

        data_in_param_name = action['data_in']
        validate_data_in_param(action_params, data_in_param_name, '{0} action {1}'.format(action_type, action_name))
        if action_type == 'Condition':
            function_ = get_condition(app_name, action['run'])
        else:
            function_ = get_transform(app_name, action['run'])
        validate_action_params(action_params, dereferencer, action_type, action_name, function_)
        seen.add(action['run'])

    if seen != set(defined_actions):
        logger.warning('Global {0}s have defined the following actions which do not have a corresponding API: '
                       '{1}'.format(action_type.lower(), (set(defined_actions) - seen)))


def validate_spec_json(spec, schema_path, spec_url='', http_handlers=None):
    schema_path = os.path.abspath(schema_path)
    with open(schema_path, 'r') as schema_file:
        schema = json.loads(schema_file.read())
    schema_resolver = RefResolver('file://{}'.format(schema_path), schema)
    spec_resolver = RefResolver(spec_url, spec, handlers=http_handlers or {})

    ref_validators.validate(spec,
                            schema,
                            resolver=schema_resolver,
                            instance_cls=ref_validators.create_dereffing_validator(spec_resolver),
                            cls=Draft4Validator)
    return spec_resolver


def validate_actions(actions, dereferencer, app_name):
    from walkoff.appgateway import get_app_action
    from walkoff.appgateway import get_all_actions_for_app
    defined_actions = get_all_actions_for_app(app_name)
    seen = set()
    for action_name, action in actions.items():
        if action['run'] not in defined_actions:
            raise InvalidApi('Action {0} has "run" property {1} '
                             'which is not defined in App {2}'.format(action_name, action['run'], app_name))
        action = dereferencer(action)
        action_params = dereferencer(action.get('parameters', []))
        event = action.get('event', '')
        if action_params:
            validate_action_params(action_params, dereferencer, app_name,
                                   action_name, get_app_action(app_name, action['run']), event=event)
        if 'default_return' in action:
            if action['default_return'] not in action.get('returns', []):
                raise InvalidApi(
                    'Default return {} not in defined return codes {}'.format(
                        action['default_return'], action.get('returns', []).keys()))

        validate_app_action_return_codes(action.get('returns', []), app_name, action_name)
        seen.add(action['run'])
    if seen != set(defined_actions):
        logger.warning('App {0} has defined the following actions which do not have a corresponding API: '
                       '{1}'.format(app_name, (set(defined_actions) - seen)))


def validate_action_params(parameters, dereferencer, app_name, action_name, action_func, event=''):
    seen = set()
    for parameter in parameters:
        parameter = deref(parameter, dereferencer)
        name = parameter['name']
        if name in seen:
            raise InvalidApi('Duplicate parameter {0} in api for {1} '
                             'for action {2}'.format(name, app_name, action_name))
        seen.add(name)

    if hasattr(action_func, '__arg_names'):
        method_params = list(action_func.__arg_names)
    else:
        method_params = get_function_arg_names(action_func)

    if method_params and method_params[0] == 'self':
        method_params.pop(0)

    if event:
        method_params.pop(0)

        if action_func.__event_name != event:
            logger.warning('In app {0} action {1}, event documented {2} does not match '
                           'event specified {3}'.format(app_name, action_name, event, action_func.__event_name))

    if not seen == set(method_params):
        only_in_api = seen - set(method_params)
        only_in_definition = set(method_params) - seen
        message = ('Discrepancy between defined parameters in API and in method definition '
                   'for app {0} action {1}.'.format(app_name, action_name))
        if only_in_api:
            message += ' Only in API: {0}.'.format(only_in_api)
        if only_in_definition:
            message += ' Only in definition: {0}'.format(only_in_definition)
        raise InvalidApi(message)


def validate_app_action_return_codes(return_codes, app, action):
    reserved = [return_code for return_code in return_codes if return_code in reserved_return_codes]
    if reserved:
        message = 'App {0} action {1} has return codes {2} which are reserved'.format(app, action, reserved)
        logger.error(message)
        raise InvalidApi(message)


def validate_definition(definition, dereferencer, definition_name=None):
    definition = dereferencer(definition)

    if 'allOf' in definition:
        for inner_definition in definition['allOf']:
            validate_definition(inner_definition, dereferencer)
    else:
        required = definition.get('required', [])
        properties = definition.get('properties', {}).keys()
        extra_properties = list(set(required) - set(properties))
        if extra_properties:
            raise InvalidApi("Required list of properties for definition "
                             "{0} not defined: {1}".format(definition_name, extra_properties))


def validate_definitions(definitions, dereferencer):
    for definition_name, definition in definitions.items():
        validate_definition(definition, dereferencer, definition_name)


def handle_user_roles_validation(param):
    param.type = 'integer'
    if not hasattr(param, 'minimum'):
        param.minimum = 1


def validate_primitive_parameter(value, param, parameter_type, message_prefix, hide_input=False):
    try:
        converted_value = convert_primitive_type(value, parameter_type)
    except (ValueError, TypeError):
        message = '{0} has invalid input. ' \
                  'Input {1} could not be converted to type {2}'.format(message_prefix, value, parameter_type)
        logger.error(message)
        raise InvalidArgument(message)
    else:
        param = deepcopy(param)
        if param.type in ('user', 'role'):
            handle_user_roles_validation(param)

        try:
            Draft4Validator(
                param.__dict__, format_checker=draft4_format_checker).validate(converted_value)
        except ValidationError as exception:
            if not hide_input:
                message = '{0} has invalid input. ' \
                          'Input {1} with type {2} does not conform to ' \
                          'validators: {3}'.format(message_prefix, value, parameter_type,
                                                   format_exception_message(exception))
            else:
                message = '{0} has invalid input. {1} does not conform to ' \
                          'validators: {2}'.format(message_prefix, parameter_type,
                                                   format_exception_message(exception))
            logger.error(message)
            raise InvalidArgument(message)
        return converted_value


def validate_parameter(value, param, message_prefix):
    param = deepcopy(param)
    primitive_type = 'primitive' if hasattr(param, 'type') else 'object'
    converted_value = None
    if value is not None:
        if primitive_type == 'primitive':
            primitive_type = param.type
            if primitive_type in TYPE_MAP:
                converted_value = validate_primitive_parameter(value, param, primitive_type, message_prefix)
            elif primitive_type == 'array':
                try:
                    converted_value = convert_array(param, value, message_prefix)
                    if hasattr(param, 'items') and param.items['type'] in ('user', 'role'):
                        handle_user_roles_validation(param.items)

                    Draft4Validator(
                        param.__dict__, format_checker=draft4_format_checker).validate(converted_value)
                except ValidationError as exception:
                    message = '{0} has invalid input. Input {1} does not conform to ' \
                              'validators: {2}'.format(message_prefix, value, format_exception_message(exception))
                    logger.error(message)
                    raise InvalidArgument(message)
            else:
                raise InvalidArgument('In {0}: Unknown parameter type {1}'.format(message_prefix, primitive_type))
        else:
            try:
                converted_value = convert_json(param, value, message_prefix)
                Draft4Validator(
                    param.schema.__dict__, format_checker=draft4_format_checker).validate(converted_value)
            except ValidationError as exception:
                message = '{0} has invalid input. Input {1} does not conform to ' \
                          'validators: {2}'.format(message_prefix, value, format_exception_message(exception))
                logger.error(message)
                raise InvalidArgument(message)
    elif param.required:
        message = "In {0}: Missing {1} parameter '{2}'".format(message_prefix, primitive_type, param.name)
        logger.error(message)
        raise InvalidArgument(message)

    return converted_value


def validate_parameters(api, arguments, message_prefix, accumulator=None):
    api_dict = {}
    for param in api:
        api_dict[param.name] = param
    converted = {}
    seen_params = set()
    arg_names = [argument.name for argument in arguments] if arguments else []
    arguments_set = set(arg_names)
    errors = []
    for param_name, param_api in api_dict.items():
        try:
            argument = get_argument_by_name(arguments, param_name)
            if argument:
                arg_val = argument.get_value(accumulator)
                if accumulator or not argument.is_ref:
                    converted[param_name] = validate_parameter(arg_val, param_api, message_prefix)
            elif hasattr(param_api, 'default'):
                try:
                    default_param = validate_parameter(param_api.default, param_api, message_prefix)
                except InvalidArgument as e:
                    default_param = param_api.default
                    logger.warning(
                        'For {0}: Default input {1} (value {2}) does not conform to schema. (Error: {3})'
                        'Using anyways'.format(message_prefix, param_name, param_api.default,
                                               format_exception_message(e)))

                converted[param_name] = default_param
                arguments_set.add(param_name)
            elif param_api.required:
                message = 'For {0}: Parameter {1} is not specified and has no default'.format(message_prefix,
                                                                                              param_name)
                logger.error(message)
                raise InvalidArgument(message)
            else:
                converted[param_name] = None
                arguments_set.add(param_name)
            seen_params.add(param_name)
        except InvalidArgument as e:
            errors.append(e.message)
    if seen_params != arguments_set:
        message = 'For {0}: Too many arguments. Extra arguments: {1}'.format(message_prefix,
                                                                             arguments_set - seen_params)
        logger.error(message)
        errors.append(message)
    if errors:
        raise InvalidArgument('Invalid arguments', errors=errors)
    return converted


def get_argument_by_name(arguments, name):
    for argument in arguments:
        if argument.name == name:
            return argument
    return None


def validate_app_action_parameters(api, arguments, app, action, accumulator=None):
    message_prefix = 'app {0} action {1}'.format(app, action)
    return validate_parameters(api, arguments, message_prefix, accumulator)


def validate_condition_parameters(api, arguments, condition, accumulator=None):
    return validate_parameters(api, arguments, 'condition {0}'.format(condition), accumulator)


def validate_transform_parameters(api, arguments, transform, accumulator=None):
    return validate_parameters(api, arguments, 'transform {0}'.format(transform), accumulator)


def validate_device_field(field_api, value, message_prefix):
    field_type = field_api.type
    field_api = deepcopy(field_api)

    # Necessary for optional fields
    if not field_api.required and (value == '' or value is None):
        return

    if field_api.encrypted:
        hide = True
    else:
        hide = False
    validate_primitive_parameter(value, field_api, field_type, message_prefix, hide_input=hide)


def validate_devices_api(devices_api, app_name):
    for device_type, device_type_api in devices_api.items():
        for field_api in device_type_api.fields:
            if hasattr(field_api, 'default'):
                message_prefix = 'App {0} device type {1}'.format(app_name, device_type)
                default_value = field_api.default
                try:
                    validate_device_field(field_api, default_value, message_prefix)
                except InvalidArgument as e:
                    logger.error(
                        'For {0}: Default input {1} does not conform to schema. (Error: {2})'
                        'Using anyways'.format(message_prefix, field_api.name, format_exception_message(e)))
                    raise


def validate_device_fields(device_fields_api, device_fields, device_type, app, validate_required=True):
    message_prefix = 'Device type {0} for app {1}'.format(device_type, app)

    for field_api in device_fields_api:
        if field_api.name not in device_fields and hasattr(field_api, 'default'):
            device_fields[field_api.name] = field_api.default

    required_in_api = {field.name for field in device_fields_api if field.required}
    field_names = set(device_fields)
    if validate_required and (required_in_api - field_names):
        message = '{0} requires {1} field but only got {2}'.format(message_prefix,
                                                                   list(required_in_api), list(field_names))
        logger.error(message)
        raise InvalidArgument(message)

    device_fields_api_dict = {field.name: field for field in device_fields_api}

    for field, value in device_fields.items():
        if field in device_fields_api_dict:
            validate_device_field(device_fields_api_dict[field], value, message_prefix)
        else:
            message = '{0} was passed field {1} which is not defined in its API'.format(message_prefix, field.name)
            logger.warning(message)
            raise InvalidArgument(message)

    return device_fields
