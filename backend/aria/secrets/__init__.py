"""SecretProvider — единый интерфейс получения секретов.

Принцип (D1 ТЗ v13): слой, который не сериализуется при tar czf.
KeyPool не читает .env напрямую, а запрашивает ключи у SecretProvider,
который знает источник (env, encrypted file, keyring, vault), но сам
никогда не попадает в архив.

Использование:
    from aria.secrets import secret_provider
    keys = secret_provider.get_key_list("GEMINI_API_KEYS")
"""
from aria.secrets.provider import EnvSecretProvider, SecretProvider

# Единственный instance на процесс. Заменить на другой класс
# (FileSecretProvider, KeyringProvider) без изменения вызывающего кода.
secret_provider: SecretProvider = EnvSecretProvider()
