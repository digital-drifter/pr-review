import subprocess

KEYCHAIN_TIMEOUT = 30


class SecretMissing(Exception):
    pass


def read_secret(service: str) -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SecretMissing("Keychain read for service %s timed out" % service)
    if result.returncode != 0:
        raise SecretMissing("No Keychain item for service %s" % service)
    return result.stdout.rstrip("\n")
