from django.core.management.base import BaseCommand

from indexation.services import process_next_queued


class Command(BaseCommand):
    help = ("Traite la file d'attente (UNE recherche par passage, sans pause "
            "longue, pour tache planifiee). Avec --loop : vide toute la file, "
            "JSON par JSON (pause 10s-3min entre recherches, 1h-2h entre JSON).")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true",
                            help="Vide toute la file d'un coup")
        parser.add_argument("--force", action="store_true",
                            help="Supprime un verrou abandonne avant de demarrer")

    def handle(self, *args, **options):
        if options["force"]:
            from indexation.services import release_cron_lock
            release_cron_lock()
            self.stdout.write("Verrou abandonne supprime.")
        if not options["loop"]:
            done = process_next_queued()
            if done:
                self.stdout.write(self.style.SUCCESS("Passage termine : une recherche traitee."))
            else:
                self.stdout.write("Rien a faire (file vide, verrou actif ou Drive bloque).")
            return
        from indexation.services import run_all_queued
        run_all_queued()
        self.stdout.write(self.style.SUCCESS("Boucle terminee."))
