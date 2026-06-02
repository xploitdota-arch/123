import uuid

def get_offline_uuid(username: str) -> str:
    """Генерирует UUID в формате Minecraft offline"""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"OfflinePlayer:{username}"))
