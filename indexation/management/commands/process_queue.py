import random
import time

from django.core.management.base import BaseCommand

from indexation.services import process_next_queued


class Command(BaseCommand):
    help = ("Traite la file d'attente (UNE recherche par passage, sans pause "
            "longue). Avec --loop : vide toute la file avec une pause "
            "aleatoire 10s-3min entre recherches.")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true",
                            help="Traite les recherches une par une jusqu'a vider la file")
        parser.add_argument("--force", action="store_true",
                            help="Supprime un verrou abandonne avant de demarrer")

    def handle(self, *args, **options):
        from django.conf import settings
        if options["force"]:
            from indexation.services import release_cron_lock
            release_cron_lock()
            self.stdout.write("Verrou abandonne supprime.")
        pause_max = int(getattr(settings, "QUEUE_SEARCH_PAUSE", 180))
        pause_min = int(getattr(settings, "QUEUE_SEARCH_PAUSE_MIN", 30))
        if not options["loop"]:
            done = process_next_queued()
            if done:
                self.stdout.write(self.style.SUCCESS("Passage termine : une recherche traitee."))
            else:
                self.stdout.write("Rien a faire (file vide, verrou actif ou Drive bloque).")
            return
        count = 0
        from indexation.services import next_queued_task
        while process_next_queued():
            count += 1
            if next_queued_task() is None:
                break  # file vide : pas de pause inutile a la fin
            pause = random.randint(pause_min, pause_max)
            self.stdout.write(f"Recherche {count} traitee. Pause aleatoire de {pause}s avant la suivante...")
            time.sleep(pause)
        self.stdout.write(self.style.SUCCESS(f"Boucle terminee : {count} recherche(s) traitee(s)."))
