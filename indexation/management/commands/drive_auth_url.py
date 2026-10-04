from django.core.management.base import BaseCommand, CommandError

from indexation.drive import auth_url, secret_files


class Command(BaseCommand):
    help = "Affiche le lien d'autorisation Google pour un compte (1-4)."

    def add_arguments(self, parser):
        parser.add_argument("account", type=int, help="Numero du compte (1-4)")

    def handle(self, *args, **options):
        account = options["account"]
        if not (1 <= account <= len(secret_files())):
            raise CommandError(f"Compte invalide (1-{len(secret_files())})")
        url = auth_url(account)
        self.stdout.write(f"Compte {account} : ouvrez ce lien dans le navigateur")
        self.stdout.write(f"connecte au bon compte Google, acceptez, puis copiez")
        self.stdout.write(f"le CODE dans la barre d'adresse (apres ?code=...&scope=).")
        self.stdout.write("")
        self.stdout.write(url)
