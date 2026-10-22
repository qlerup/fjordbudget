"""Keys are private files separate from the database. CLI prints public key only."""
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from community_crypto import public_key


def persistent(path,factory):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as file:
            file.write(factory())
    except FileExistsError:
        pass
    return path.read_bytes()


class Vault:
    def __init__(self,directory):
        root=Path(directory)
        directory=root/'keys'
        if (root/'community.sqlite3').exists() and any(not (directory/name).exists() for name in ('transport.key','storage.key','index.key')):
            raise ValueError('Databasens krypteringsnøgler mangler. Gendan nøglerne fra backup; der oprettes ikke nye oven på eksisterende data.')
        self.private=X25519PrivateKey.from_private_bytes(persistent(directory/'transport.key',lambda:X25519PrivateKey.generate().private_bytes_raw()))
        self.cipher=Fernet(persistent(directory/'storage.key',Fernet.generate_key))
        self.index_key=persistent(directory/'index.key',lambda:os.urandom(32))

    @property
    def public_key(self):
        return public_key(self.private)

    def encrypt(self,value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self,value):
        return self.cipher.decrypt(value.encode()).decode()

    def index(self,value):
        return hmac.new(self.index_key,value.encode(),hashlib.sha256).hexdigest()


if __name__=='__main__':
    import sys
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python -m fjordbudget_server.keys /var/lib/fjordbudget-server')
    print(Vault(sys.argv[1]).public_key)
