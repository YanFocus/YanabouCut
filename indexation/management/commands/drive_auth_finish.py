from django.core.management.base import BaseCommand, CommandError

from indexation.drive import account_email, finish_auth, get_service, secret_files


class Command(BaseCommand):
    help = "Termine l'autorisation Google d'un compte a partir du code colle."

    def add_arguments(self, parser):
        parser.add_argument("account", type=int, help="Numero du compte (1-4)")
        parser.add_argument("code", help="Code d'autorisation copie dans le navigateur")

    def handle(self, *args, **options):
        account = options["account"]
        if not (1 <= account <= len(secret_files())):
            raise CommandError(f"Compte invalide (1-{len(secret_files())})")
        try:
            finish_auth(account, options["code"])
            email = account_email(get_service(account))
        except Exception as e:
            raise CommandError(f"Echec : {e}")
        self.stdout.write(self.style.SUCCESS(f"Compte {account} autorise : {email}"))
