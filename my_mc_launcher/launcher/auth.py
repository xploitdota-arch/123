import hashlib

def get_offline_uuid(username):
    hash_obj = hashlib.md5(username.lower().encode())
    return f"00000000-0000-0000-0000-{hash_obj.hexdigest()[-12:]}"
