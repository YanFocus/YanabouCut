"""Diagnostic connectivite Drive : proxy effectif + quota par compte."""
import os

from django.core.management.base import BaseCommand

from indexation.drive import (
    _http_client,
    account_email,
    get_service,
    quota_info,
    secret_files,
)


class Command(BaseCommand):
    help = "Teste la connexion Drive de chaque compte (proxy, auth, quota)."

    def handle(self, *args, **options):
        self.stdout.write(f"http_proxy={os.environ.get('http_proxy')}")
        self.stdout.write(f"https_proxy={os.environ.get('https_proxy')}")
        http = _http_client()
        self.stdout.write(f"proxy httplib2 : {getattr(http, 'proxy_info', None)}")
        for account in range(1, len(secret_files()) + 1):
            try:
                service = get_service(account)
                email = account_email(service)
                q = quota_info(service)
                if q["limited"]:
                    self.stdout.write(self.style.SUCCESS(
                        f"Compte {account} ({email}) : OK, {q['usage'] // 1024 // 1024}/{q['limit'] // 1024 // 1024} Mo"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"Compte {account} ({email}) : OK, quota illimite"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Compte {account} : ECHEC ({str(e)[:200]})"))
