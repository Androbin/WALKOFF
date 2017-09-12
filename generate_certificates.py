import os
import shutil
import zmq.auth


def generate_certificates(base_dir):
    keys_dir = os.path.join(base_dir, 'certificates')
    public_keys_dir = os.path.join(base_dir, 'public_keys')
    secret_keys_dir = os.path.join(base_dir, 'private_keys')

    # Create dirs for certs, remove old content if necessary
    for d in [keys_dir, public_keys_dir, secret_keys_dir]:
        if os.path.exists(d):
            shutil.rmtree(d)
        os.mkdir(d)

    # Create new keys in certs dir
    server_public_file, server_secret_file = zmq.auth.create_certificates(keys_dir, "server")
    client_public_file, client_secret_file = zmq.auth.create_certificates(keys_dir, "client")

    # Move public keys to appropriate dir
    for key_file in os.listdir(keys_dir):
        if key_file.endswith(".key"):
            shutil.move(os.path.join(keys_dir, key_file), os.path.join(public_keys_dir, '.'))

    # Move secret keys to appropriate dir
    for key_file in os.listdir(keys_dir):
        if key_file.endswith(".key_secret"):
            shutil.move(os.path.join(keys_dir, key_file), os.path.join(secret_keys_dir, '.'))


if __name__ == '__main__':
    generate_certificates(os.path.dirname(__file__))
