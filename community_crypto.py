"""Authenticated app-to-server envelopes; pinned recipient key, no plaintext fallback.

X25519 + HKDF-SHA256 derives independent AES-256-GCM keys for each direction.
Every exchange has a fresh ephemeral key and is bound to the request's public key.
TLS is still required for metadata privacy and the administrator's browser session.
"""
import base64
import json
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DOMAIN=b'fjordbudget-community-envelope-v1'


def encode(value):
    return base64.b64encode(value).decode('ascii')


def decode(value,length=None):
    if not isinstance(value,str) or len(value)>700000:
        raise ValueError('Ugyldig krypteret besked.')
    try:
        result=base64.b64decode(value,validate=True)
    except (ValueError,TypeError):
        raise ValueError('Ugyldig krypteret besked.') from None
    if length is not None and len(result)!=length:
        raise ValueError('Ugyldig nøgle eller nonce.')
    return result


def public_key(private):
    return encode(private.public_key().public_bytes_raw())


def derive(private,peer,salt,ephemeral,recipient):
    shared=private.exchange(X25519PublicKey.from_public_bytes(peer))
    material=HKDF(algorithm=hashes.SHA256(),length=64,salt=salt,
        info=DOMAIN+ephemeral+recipient).derive(shared)
    return material[:32],material[32:]


def seal(key,data,aad):
    nonce=os.urandom(12)
    content=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
    return {'nonce':encode(nonce),'box':encode(AESGCM(key).encrypt(nonce,content,aad))}


def unseal(key,envelope,aad):
    if not isinstance(envelope,dict) or set(envelope)!={'nonce','box'}:
        raise ValueError('Ugyldig krypteret besked.')
    try:
        result=json.loads(AESGCM(key).decrypt(decode(envelope['nonce'],12),decode(envelope['box']),aad))
    except (InvalidTag,UnicodeDecodeError,ValueError):
        raise ValueError('Beskeden kunne ikke dekrypteres eller verificeres.') from None
    if not isinstance(result,dict):
        raise ValueError('Ugyldig krypteret besked.')
    return result


def make_request(pinned_public_key,method,path,token,body):
    ephemeral=X25519PrivateKey.generate()
    epk=ephemeral.public_key().public_bytes_raw()
    recipient=decode(pinned_public_key,32)
    salt=os.urandom(32)
    request_key,response_key=derive(ephemeral,recipient,salt,epk,recipient)
    content={'method':method,'path':path,'token':token,'body':body,'issued_at':int(time.time())}
    sealed=seal(request_key,content,DOMAIN+b'/request'+epk)
    return {'v':1,'epk':encode(epk),'salt':encode(salt),**sealed},(response_key,epk)


def open_request(private,envelope):
    if not isinstance(envelope,dict) or set(envelope)!={'v','epk','salt','nonce','box'} or envelope['v']!=1:
        raise ValueError('Krypteret protokol v1 kræves.')
    epk=decode(envelope['epk'],32)
    request_key,response_key=derive(private,epk,decode(envelope['salt'],32),epk,private.public_key().public_bytes_raw())
    body=unseal(request_key,{'nonce':envelope['nonce'],'box':envelope['box']},DOMAIN+b'/request'+epk)
    if set(body)!={'method','path','token','body','issued_at'} or type(body['issued_at']) is not int or abs(time.time()-body['issued_at'])>300:
        raise ValueError('Beskeden er udløbet eller ugyldig. Kontrollér serverens ur.')
    return body,(response_key,epk)


def make_response(context,data):
    key,epk=context
    return seal(key,data,DOMAIN+b'/response'+epk)


def open_response(context,data):
    key,epk=context
    return unseal(key,data,DOMAIN+b'/response'+epk)
