"""Teste chaque compte autorise contre chaque dossier et reconstitue l'appariement."""
from django.core.management.base import BaseCommand

from indexation.drive import (
    account_email,
    check_folder,
    get_service,
    load_folders,
    quota_info,
    save_mapping,
    secret_files,
)


class Command(BaseCommand):
    help = "Verifie acces/quota de chaque compte sur chaque dossier et genere drive_mapping.json."

    def handle(self, *args, **options):
        folders = load_folders()
        mapping = []
        for account in range(1, len(secret_files()) + 1):
            try:
                service = get_service(account)
            except Exception as e:
                self.stdout.write(f"Compte {account} : PAS DE TOKEN ({str(e)[:100]}) -> autorisez-le d'abord.")
                continue
            email = account_email(service)
            q = quota_info(service)
            if q["limited"]:
                self.stdout.write(f"Compte {account} ({email}) : quota {q['usage'] // 1024 // 1024} / {q['limit'] // 1024 // 1024} Mo")
            else:
                self.stdout.write(f"Compte {account} ({email}) : quota illimite")
            accessible = []
            for f in folders:
                chk = check_folder(service, f["folder_id"])
                flag = "OK ecriture" if (chk.get("ok") and chk.get("writable")) else ("lecture seule" if chk.get("ok") else "PAS D'ACCES")
                self.stdout.write(f"  - {f['label']} : {flag}")
                if chk.get("ok") and chk.get("writable"):
                    accessible.append((f, chk))
            if not accessible:
                self.stdout.write(self.style.WARNING(f"Compte {account} : aucun dossier accessible en ecriture !"))
                continue
            # Preference au dossier du meme numero si accessible, sinon premier accessible
            chosen = next((f for f, _ in accessible if f["label"].endswith(f" {account}")), accessible[0][0])
            mapping.append({
                "account": account,
                "email": email,
                "folder_id": chosen["folder_id"],
                "folder_label": chosen["label"],
            })
        mapping.sort(key=lambda m: m["account"])
        save_mapping(mapping)
        self.stdout.write(self.style.SUCCESS(f"Mapping enregistre ({len(mapping)} compte(s)) :"))
        for m in mapping:
            self.stdout.write(f"  Compte {m['account']} ({m['email']}) -> {m['folder_label']}")
