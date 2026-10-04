"""Diagnostic connectivite Drive : proxy effectif + quota par compte."""
import os

from django.core.management.base import BaseCommand

from indexation import drive
from indexation.drive import get_service, secret_files


class Command(BaseCommand):
    help = "Teste la connexion Drive de chaque compte (proxy, auth, quota)."

    def handle(self, *args, **options):
        self.stdout.write(f"http_proxy={os.environ.get('http_proxy')}")
        self.stdout.write(f"https_proxy={os.environ.get('https_proxy')}")
        self.stdout.write(f"proxy effectif : {drive._proxy_dict() or 'AUCUN (connexion directe)'}")
        for account in range(1, len(secret_files()) + 1):
            try:
                api = get_service(account)
                email = api.email()
                q = api.quota()
                if q["limited"]:
                    self.stdout.write(self.style.SUCCESS(
                        f"Compte {account} ({email}) : OK, {q['usage'] // 1024 // 1024}/{q['limit'] // 1024 // 1024} Mo"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"Compte {account} ({email}) : OK, quota illimite"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Compte {account} : ECHEC ({str(e)[:200]})"))
