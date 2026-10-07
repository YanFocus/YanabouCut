"""Ajoute la duree (secondes) aux videos du JSON qui n'en ont pas.

Source 1 : API Pixabay par ID (duree exacte d'origine).
Source 2 (repli) : metadonnees Drive (durationMillis).
Sauvegarde incrementale toutes les 25 videos (reprise sans perte).
Necessite PIXABAY_API_KEY dans l'environnement.
"""
import json
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Remplit la duree manquante des videos du JSON (Pixabay puis Drive)."

    def handle(self, *args, **options):
        from indexation import drive

        site = Path(settings.SITE_JSON_PATH)
        shutil.copy2(site, site.with_suffix(".avant_duree.json"))
        self.stdout.write("Sauvegarde : json_du_site.avant_duree.json")

        state = json.loads(site.read_text(encoding="utf-8"))
        videos = state.get("videos", [])
        manquantes = [v for v in videos if v.get("duree") is None]
        self.stdout.write(f"Videos : {len(videos)} | sans duree : {len(manquantes)}")
        if not manquantes:
            self.stdout.write(self.style.SUCCESS("RIEN A FAIRE"))
            return

        api_key = getattr(settings, "PIXABAY_API_KEY", "")
        if not api_key:
            self.stdout.write(self.style.ERROR("PIXABAY_API_KEY manquante dans l'environnement."))
            return
        ok_pixabay = ok_drive = echecs = 0

        drive_durees = {}
        try:
            for m in drive.load_mapping():
                a = drive.DriveAPI(m["account"])
                for f in a.list_files(m["folder_id"]):
                    meta = f.get("videoMediaMetadata") or {}
                    if f.get("name", "").endswith(".mp4") and meta.get("durationMillis"):
                        drive_durees[f["name"]] = round(int(meta["durationMillis"]) / 1000)
            self.stdout.write(f"Index Drive : {len(drive_durees)} videos referencees")
        except Exception as e:
            self.stdout.write(f"Index Drive impossible : {e} (repli desactive)")

        for i, v in enumerate(manquantes, 1):
            uid = str(v.get("identifiant_unique"))
            duree = None
            try:
                url = f"https://pixabay.com/api/videos/?key={api_key}&id={urllib.parse.quote_plus(uid)}"
                req = urllib.request.Request(url, headers={"User-Agent": "YanabouCut/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    hits = json.loads(resp.read().decode("utf-8")).get("hits", [])
                if hits and hits[0].get("duration") is not None:
                    duree = hits[0]["duration"]
                    ok_pixabay += 1
            except Exception as e:
                self.stdout.write(f"  ID {uid} : Pixabay HS ({str(e)[:80]})")
            time.sleep(0.7)  # sous la limite 100 req/60s
            if duree is None:
                duree = drive_durees.get(f"{v.get('nouveau_nom')}.mp4")
                if duree is not None:
                    ok_drive += 1
            if duree is None:
                echecs += 1
                self.stdout.write(f"  [{i}/{len(manquantes)}] ID {uid} : duree introuvable, laisse vide")
            else:
                v["duree"] = duree
            if i % 25 == 0:
                site.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                self.stdout.write(f"  ... {i}/{len(manquantes)} (pixabay={ok_pixabay}, drive={ok_drive}, echecs={echecs})")

        site.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        final = json.loads(site.read_text(encoding="utf-8"))
        restantes = sum(1 for v in final["videos"] if v.get("duree") is None)
        self.stdout.write(self.style.SUCCESS(
            f"TERMINE : pixabay={ok_pixabay}, drive={ok_drive}, echecs={echecs}, sans duree restantes={restantes}"))
