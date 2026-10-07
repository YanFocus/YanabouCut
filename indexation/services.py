"""Logique metier YanabouCut : recherche Pixabay, telechargement, json_du_site.json."""
import json
import os
import random
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import logging

logger = logging.getLogger(__name__)

_job_lock = threading.Lock()
_job_state = {
    "running": False,
    "queue_running": False,
    "logs": [],
    "current_search": None,
    "current_file": None,
    "current_video": None,
    "current_done": 0,
    "current_target": 0,
    "started_at": None,
    "finished_at": None,
}

STATUS_FILENAME = "job_status.json"


def _now_str():
    return datetime.now().strftime("%H:%M:%S")


def get_paths():
    from django.conf import settings
    site_json = Path(getattr(settings, "SITE_JSON_PATH"))
    videos_dir = Path(getattr(settings, "VIDEOS_DIR"))
    status_path = site_json.parent / STATUS_FILENAME
    videos_dir.mkdir(parents=True, exist_ok=True)
    return site_json, videos_dir, status_path


def load_site_json():
    site_json, _, _ = get_paths()
    for candidate in (site_json, site_json.with_suffix(".bak.json")):
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                data.setdefault("counter", 0)
                data.setdefault("videos", [])
                return data
            except Exception:
                logger.exception(f"Lecture {candidate} impossible, essai du backup")
                continue
    return {"counter": 0, "videos": []}


def save_site_json(state):
    import shutil
    site_json, _, _ = get_paths()
    # Sauvegarde de la version precedente avant ecrasement (anti-perte)
    if site_json.exists():
        try:
            shutil.copy2(site_json, site_json.with_suffix(".bak.json"))
        except OSError:
            pass
    tmp = site_json.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, site_json)


def _push_log(message, level="info"):
    entry = {"time": _now_str(), "level": level, "message": message}
    with _job_lock:
        _job_state["logs"].append(entry)
        # garde les 500 derniers pour ne pas gonfler la memoire
        if len(_job_state["logs"]) > 500:
            _job_state["logs"] = _job_state["logs"][-500:]
    _persist_status()
    return entry


def _persist_status():
    try:
        _, _, status_path = get_paths()
        with _job_lock:
            snapshot = {
                "running": _job_state["running"],
                "queue_running": _job_state["queue_running"],
                "current_search": _job_state["current_search"],
                "current_file": _job_state["current_file"],
                "current_video": _job_state["current_video"],
                "current_done": _job_state["current_done"],
                "current_target": _job_state["current_target"],
                "started_at": _job_state["started_at"],
                "finished_at": _job_state["finished_at"],
                "logs": _job_state["logs"][-200:],
            }
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clear_logs():
    """Efface le journal (appele a chaque nouveau JSON uploade : le journal
    n'affiche ensuite que le JSON en cours)."""
    with _job_lock:
        _job_state["logs"] = []
    _persist_status()


def get_job_status():
    with _job_lock:
        return {
            "running": _job_state["running"],
            "queue_running": _job_state["queue_running"],
            "current_search": _job_state["current_search"],
            "current_file": _job_state["current_file"],
            "current_video": _job_state["current_video"],
            "current_done": _job_state["current_done"],
            "current_target": _job_state["current_target"],
            "started_at": _job_state["started_at"],
            "finished_at": _job_state["finished_at"],
            "logs": list(_job_state["logs"][-200:]),
        }


def is_running():
    with _job_lock:
        return _job_state["running"]


def pixabay_search(query, api_key, per_page=50):
    """Appelle l'API videos Pixabay et retourne la liste brute des hits."""
    encoded = urllib.parse.quote_plus(query)
    url = (
        f"https://pixabay.com/api/videos/?key={api_key}"
        f"&q={encoded}&per_page={per_page}&safesearch=true"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "YanabouCut/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("hits", [])


def pick_best_rendition(videos_dict):
    """Choisit une rendition utilisable (medium > large > small > tiny)."""
    if not isinstance(videos_dict, dict):
        return None
    for key in ("medium", "large", "small", "tiny"):
        info = videos_dict.get(key)
        if info and info.get("url"):
            return info
    return None


def is_16_9(width, height):
    if not height:
        return False
    ratio = width / height
    return 1.7 <= ratio <= 1.85


def filter_hits(hits):
    """Filtre : duree < 13s ET format 16:9. Retourne liste normalisee dans l'ordre API."""
    kept = []
    for hit in hits:
        try:
            duration = hit.get("duration", 0)
            if duration is None or duration >= 13:
                continue
            rendition = pick_best_rendition(hit.get("videos", {}))
            if not rendition:
                continue
            w = rendition.get("width", 0) or 0
            h = rendition.get("height", 0) or 0
            if not is_16_9(w, h):
                continue
            kept.append({
                "id": hit.get("id"),
                "tags": hit.get("tags", "") or "",
                "duration": duration,
                "width": w,
                "height": h,
                "url": rendition.get("url"),
                "thumbnail": rendition.get("thumbnail"),
                "user": hit.get("user", ""),
                "pageURL": hit.get("pageURL", ""),
            })
        except Exception:
            continue
    return kept


def download_video(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": "YanabouCut/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    with open(dest_path, "wb") as f:
        f.write(data)
    return len(data)


def run_job(searches, wait_seconds=None):
    """Boucle principale executee en tache de fond (thread daemon).

    searches: dict ordonne {recherche1: query, ...}
    Pour chaque recherche :
      - tirage aleatoire 2-6 = objectif
      - parcours des resultats filtres dans l'ordre, skip si ID deja dans json
      - download + renommage video_N (compteur persistant) + enregistrement
        (nouveau_nom, identifiant_unique, tags uniquement)
      - pause aleatoire 2-10s entre chaque video
      - si epuise (0..5 obtenus) -> recherche suivante
      - pause aleatoire 10s-3min entre recherches (sauf apres la derniere)
    A la fin de la derniere recherche : arret total, attente d'un nouveau JSON.
    wait_seconds : si defini (tests), force la pause inter-recherches ;
      sinon tirage aleatoire 60-900s.
    """
    from django.conf import settings
    api_key = getattr(settings, "PIXABAY_API_KEY", "")
    import socket
    # Garde-fou : aucune connexion reseau ne doit pendre indefiniment
    # (DNS ou socket bloque ne respectant pas toujours les timeouts)
    try:
        socket.setdefaulttimeout(60)
    except Exception:
        pass

    with _job_lock:
        if _job_state["running"]:
            return False
        _job_state["running"] = True
        _job_state["started_at"] = datetime.now().isoformat()
        _job_state["finished_at"] = None
        _job_state["current_search"] = None
    _persist_status()

    _push_log(f"Je demarre : {len(searches)} recherche(s) a traiter.")

    site_json, videos_dir, _ = get_paths()
    keys = list(searches.keys())
    stop_requested = False

    for idx, key in enumerate(keys):
        query = searches[key]
        with _job_lock:
            _job_state["current_search"] = key
        _persist_status()

        target = random.randint(2, 6)
        with _job_lock:
            _job_state["current_target"] = target
            _job_state["current_done"] = 0
            _job_state["current_video"] = None
        _push_log(f"[{key}] Je lance la recherche \"{query}\" (objectif : {target} videos).")

        # 1. Appel API
        try:
            hits = pixabay_search(query, api_key)
            _push_log(f"[{key}] Je cherche sur Pixabay : {len(hits)} resultats.")
        except Exception as e:
            _push_log(f"[{key}] ERREUR Pixabay : {e}", level="error")
            continue

        # 2. Filtrage <13s + 16:9
        filtered = filter_hits(hits)
        _push_log(f"[{key}] Je garde {len(filtered)} video(s) apres filtres (<13s, 16:9).")
        if not filtered:
            _push_log(f"[{key}] Aucune video valable : recherche suivante.")
            if idx < len(keys) - 1:
                pause_search = wait_seconds if wait_seconds is not None else random.randint(10, 180)
                if pause_search > 0:
                    _push_log(f"[{key}] J'attends {pause_search}s avant {keys[idx+1]}.")
                    time.sleep(pause_search)
            continue

        # 3. Parcours + dedup + download jusqu'a l'objectif
        state = load_site_json()
        existing_ids = {str(v.get("identifiant_unique")) for v in state.get("videos", [])}
        downloaded_this_search = 0

        for cand in filtered:
            if downloaded_this_search >= target:
                break
            uid = str(cand.get("id"))
            if uid in existing_ids:
                _push_log(f"[{key}] ID {uid} : la video existe deja, je passe a la suivante.")
                continue
            # Nouveau nom via compteur persistant
            new_counter = int(state.get("counter", 0)) + 1
            new_name = f"video_{new_counter}"
            with _job_lock:
                _job_state["current_video"] = f"{new_name}.mp4"
            dest = videos_dir / f"{new_name}.mp4"
            video_url = cand.get("url") or ""
            _push_log(f"[{key}] Je telecharge la video {downloaded_this_search+1}/{target} (ID {uid}) sous {new_name}.mp4")
            try:
                download_video(video_url, dest)
            except Exception as e:
                _push_log(f"[{key}] ERREUR telechargement ID {uid} : {e}", level="error")
                continue
            tags_raw = cand.get("tags", "") or ""
            entry = {
                "nouveau_nom": new_name,
                "identifiant_unique": uid,
                "tags": tags_raw,
            }
            state.setdefault("videos", []).append(entry)
            state["counter"] = new_counter
            saved_ok = False
            last_err = None
            for _ in range(3):
                try:
                    save_site_json(state)
                    saved_ok = True
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(1)
            if not saved_ok:
                # Annule en memoire aussi (sinon compteur fausse par rapport au disque)
                state["videos"].pop()
                state["counter"] = new_counter - 1
                _push_log(f"[{key}] ERREUR sauvegarde JSON : {last_err}", level="error")
                continue
            existing_ids.add(uid)
            downloaded_this_search += 1
            with _job_lock:
                _job_state["current_done"] = downloaded_this_search
            _push_log(f"[{key}] {new_name}.mp4 enregistre (ID {uid}). Compteur : {new_counter}.")

            # 4. Envoi Google Drive OBLIGATOIRE : la video suivante n'est traitee
            # que si celle-ci est uploadee ET supprimee du site.
            from indexation.drive import load_state as drive_load_state
            from indexation.drive import save_state as drive_save_state
            from indexation.drive import send_to_drive
            drive_name = f"{new_name}.mp4"
            uploaded_ok = False
            for attempt in range(1, 4):
                _push_log(f"[{key}] J'envoie {drive_name} vers Drive (essai {attempt}/3).")
                try:
                    account_used, drive_result = send_to_drive(dest, drive_name, lambda m: _push_log(f"[{key}] {m}"))
                except Exception as e:
                    account_used, drive_result = None, "erreur"
                    _push_log(f"[{key}] ERREUR Drive : {str(e)[:200]}", level="error")
                if account_used:
                    uploaded_ok = True
                    break
                if drive_result == "sature" or str(drive_result).startswith("config"):
                    reason = ("Les 4 comptes Google Drive sont satures." if drive_result == "sature"
                              else str(drive_result))
                    st = drive_load_state()
                    st["blocked"] = True
                    st["reason"] = reason
                    drive_save_state(st)
                    _push_log(f"[{key}] {new_name}.mp4 garde en local ({reason}). ARRET + JSON bloques.", level="error")
                    stop_requested = True
                    break
                _push_log(f"[{key}] Echec envoi ({drive_result}) : je reessaie la meme video.", level="error")
                time.sleep(10)
            if stop_requested:
                break
            if not uploaded_ok:
                st = drive_load_state()
                st["blocked"] = True
                st["reason"] = f"Echec d'envoi repete pour {drive_name}."
                drive_save_state(st)
                _push_log(f"[{key}] {new_name}.mp4 garde en local (3 echecs). ARRET + JSON bloques.", level="error")
                stop_requested = True
                break

            # 5. Suppression locale VERIFIEE avant de passer a la suite
            deleted_ok = False
            for _ in range(3):
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                    if not os.path.exists(dest):
                        deleted_ok = True
                        break
                except Exception:
                    time.sleep(2)
            if not deleted_ok:
                st = drive_load_state()
                st["blocked"] = True
                st["reason"] = f"Suppression locale impossible pour {drive_name}."
                drive_save_state(st)
                _push_log(f"[{key}] Sur Drive mais suppression locale impossible. ARRET anti-doublon.", level="error")
                stop_requested = True
                break
            _push_log(f"[{key}] Envoyee sur Drive (compte {account_used}) et supprimee du site.")

            if downloaded_this_search < target:
                pause_video = random.randint(2, 10)
                _push_log(f"[{key}] J'attends {pause_video}s avant la video suivante.")
                time.sleep(pause_video)

        if stop_requested:
            break
        _push_log(f"[{key}] Recherche {key} finie : {downloaded_this_search}/{target} video(s).")
        if idx < len(keys) - 1:
            pause_search = wait_seconds if wait_seconds is not None else random.randint(10, 180)
            if pause_search > 0:
                _push_log(f"J'attends {pause_search}s ({pause_search // 60} min {pause_search % 60}s) avant {keys[idx+1]}.")
                time.sleep(pause_search)

    _push_log("Fini pour toutes les recherches. Rechargez un nouveau JSON pour relancer.")
    try:
        from indexation.drive import backup_site_json
        backup_site_json(lambda m, level="info": _push_log(m, level=level))
    except Exception:
        pass
    with _job_lock:
        _job_state["running"] = False
        _job_state["current_search"] = None
        _job_state["finished_at"] = datetime.now().isoformat()
    _persist_status()
    return True


def start_job_async(searches, wait_seconds=None):
    with _job_lock:
        if _job_state["running"] or _job_state["queue_running"]:
            return False
    t = threading.Thread(target=run_job, args=(searches, wait_seconds), daemon=True)
    t.start()
    return True


def start_next_queued_async():
    """Lance UNE recherche de la file en tache de fond.
    Retourne False si un traitement tourne deja."""
    with _job_lock:
        if _job_state["running"] or _job_state["queue_running"]:
            return False
    t = threading.Thread(target=process_next_queued, daemon=True)
    t.start()
    return True


def run_all_queued():
    """Traite TOUTE la file, JSON par JSON, sans intervention.

    - Dans un JSON : recherches a la suite, pause aleatoire 10s-3min entre elles.
    - Entre deux JSON : pause aleatoire 1h-2h.
    - S'arrete si file vide ou Drive bloque (la suite attendra).
    """
    from django.conf import settings
    pause_min = int(getattr(settings, "QUEUE_SEARCH_PAUSE_MIN", 10))
    pause_max = int(getattr(settings, "QUEUE_SEARCH_PAUSE", 180))
    with _job_lock:
        if _job_state["running"] or _job_state["queue_running"]:
            return False
        _job_state["queue_running"] = True
    _persist_status()
    _push_log("Je traite tous les JSON a la suite, sans arret.")
    try:
        from indexation.drive import load_state as drive_load_state
        while True:
            task = next_queued_task()
            if task is None:
                break
            fpath = task[0]
            while True:
                t = next_queued_task()
                if t is None or t[0] != fpath:
                    break
                process_next_queued()
                if drive_load_state().get("blocked"):
                    _push_log("Drive bloque : j'arrete tout, la suite attendra.")
                    return True
                nxt = next_queued_task()
                if nxt is not None and nxt[0] == fpath:
                    pause = random.randint(pause_min, pause_max)
                    _push_log(f"J'attends {pause}s avant la recherche suivante.")
                    time.sleep(pause)
            if next_queued_task() is None:
                break
            pause = random.randint(3600, 7200)
            _push_log(f"JSON {fpath.name} fini. J'attends {pause // 3600}h {(pause % 3600) // 60}min avant le JSON suivant.")
            time.sleep(pause)
    finally:
        with _job_lock:
            _job_state["queue_running"] = False
            if not _job_state["running"]:
                _job_state["current_search"] = None
                _job_state["current_file"] = None
                _job_state["current_video"] = None
                _job_state["current_done"] = 0
                _job_state["current_target"] = 0
        _persist_status()
    return True


def start_all_queued_async():
    """Lance le traitement automatique complet en tache de fond.
    Retourne False si un traitement tourne deja."""
    with _job_lock:
        if _job_state["running"] or _job_state["queue_running"]:
            return False
    t = threading.Thread(target=run_all_queued, daemon=True)
    t.start()
    return True


# ---------------------------------------------------------------------------
# Mode file d'attente (hebergement gratuit : pas de threads, pas de longues
# pauses). L'upload web enregistre le JSON dans job_queue/pending/, puis la
# commande `python manage.py process_queue` (tache planifiee) traite UNE
# recherche par passage et quitte. L'espacement vient de la planification.
# ---------------------------------------------------------------------------

def queue_dirs():
    from django.conf import settings
    base = Path(getattr(settings, "QUEUE_DIR"))
    pending = base / "pending"
    done = base / "done"
    pending.mkdir(parents=True, exist_ok=True)
    done.mkdir(parents=True, exist_ok=True)
    return base, pending, done


def progress_path():
    base, _, _ = queue_dirs()
    return base / "progress.json"


def load_queue_progress():
    p = progress_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_queue_progress(progress):
    progress_path().write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def enqueue_searches(searches):
    """Enregistre un JSON uploade dans la file. Retourne le nom du fichier."""
    _, pending, _ = queue_dirs()
    # Microsecondes incluses : 2 uploads dans la meme seconde ne se collisionnent jamais
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{stamp}.json"
    n = 1
    while (pending / fname).exists():
        n += 1
        fname = f"{stamp}_{n}.json"
    (pending / fname).write_text(
        json.dumps({"searches": searches}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return fname


def next_queued_task():
    """Plus ancienne recherche non traitee : (fichier, cle, requete) ou None."""
    _, pending, _ = queue_dirs()
    progress = load_queue_progress()
    for fpath in sorted(pending.glob("*.json")):
        try:
            searches = json.loads(fpath.read_text(encoding="utf-8")).get("searches", {})
        except Exception:
            continue
        done_keys = set(progress.get(fpath.name, []))
        for key in searches:
            if key not in done_keys:
                return fpath, key, searches[key]
    return None


def mark_search_done(fpath, key):
    progress = load_queue_progress()
    done_keys = progress.setdefault(fpath.name, [])
    if key not in done_keys:
        done_keys.append(key)
    try:
        searches = json.loads(fpath.read_text(encoding="utf-8")).get("searches", {})
    except Exception:
        searches = {}
    if all(k in done_keys for k in searches):
        _, _, done_dir = queue_dirs()
        dest = done_dir / fpath.name
        n = 1
        while dest.exists():
            dest = done_dir / f"{fpath.stem}_{n}{fpath.suffix}"
            n += 1
        try:
            fpath.rename(dest)
        except OSError:
            pass
    save_queue_progress(progress)


def load_status_file():
    """Etat ecrit par _persist_status (utilise par la page web en mode queue,
    car le processus planifie est separe du processus web)."""
    _, _, status_path = get_paths()
    default = {"running": False, "queue_running": False, "current_search": None,
               "current_file": None, "current_video": None, "current_done": 0,
               "current_target": 0, "started_at": None, "finished_at": None, "logs": []}
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            default.update({k: data.get(k, default[k]) for k in default})
            if not isinstance(default["logs"], list):
                default["logs"] = []
        except Exception:
            pass
    return default


def acquire_cron_lock(max_age_hours=1):
    """Verrou fichier anti-chevauchement. Retourne True si acquis.
    Un verrou plus vieux que max_age_hours est considere comme abandonne
    (plantage) et repris. Retourne (acquis, age_secondes)."""
    base, _, _ = queue_dirs()
    lock = base / ".lock"
    age = None
    if lock.exists():
        try:
            age = (datetime.now() - datetime.fromisoformat(lock.read_text(encoding="utf-8").strip())).total_seconds()
            if age < max_age_hours * 3600:
                return False, age
        except Exception:
            return False, age
    try:
        lock.write_text(datetime.now().isoformat(), encoding="utf-8")
        return True, age
    except OSError:
        return False, age


def queue_overview():
    """Synthese pour la barre de progression : fichiers (nom, fait, total),
    progression globale et element en cours (fichier, recherche, video)."""
    _, pending_dir, done_dir = queue_dirs()
    progress = load_queue_progress()
    files = []
    done_searches = 0
    total_searches = 0
    for fpath in sorted(pending_dir.glob("*.json")):
        try:
            searches = json.loads(fpath.read_text(encoding="utf-8")).get("searches", {})
        except Exception:
            searches = {}
        done_keys = [k for k in searches if k in progress.get(fpath.name, [])]
        files.append({"name": fpath.name, "done": len(done_keys), "total": len(searches),
                      "current": False})
        done_searches += len(done_keys)
        total_searches += len(searches)
    for fpath in sorted(done_dir.glob("*.json")):
        try:
            n = len(json.loads(fpath.read_text(encoding="utf-8")).get("searches", {}))
        except Exception:
            n = 0
        files.append({"name": fpath.name, "done": n, "total": n, "current": False})
        done_searches += n
        total_searches += n
    cur = _current_state()
    cur_file = cur["file"]
    for f in files:
        if cur_file and f["name"] == cur_file:
            f["current"] = True
    pct = round(done_searches * 100 / total_searches) if total_searches else 0
    pending_count = sum(1 for f in files if f["done"] < f["total"])
    if cur["active"] and (cur["file"] or cur["search"] or cur["video"]):
        parts = [p for p in (cur["file"], cur["search"], cur["video"]) if p]
        status_line = "En cours : " + " → ".join(parts)
        if cur["target"]:
            status_line += f" ({cur['done']}/{cur['target']} videos)"
    elif pending_count:
        status_line = f"{pending_count} JSON en attente de traitement."
    elif total_searches:
        status_line = "Tout est termine."
    else:
        status_line = "En attente."
    return {"files": files, "done_searches": done_searches,
            "total_searches": total_searches, "pct": pct, "current": cur,
            "pending_count": pending_count, "status_line": status_line}


def _current_state():
    """Etat courant : fichier en mode file (processus separe), memoire sinon."""
    from django.conf import settings
    if getattr(settings, "PIPELINE_MODE", "thread") == "queue":
        st = load_status_file()
        return {
            "file": st.get("current_file"),
            "search": st.get("current_search"),
            "video": st.get("current_video"),
            "done": st.get("current_done", 0),
            "target": st.get("current_target", 0),
            "active": bool(st.get("running") or st.get("queue_running")),
        }
    with _job_lock:
        return {
            "file": _job_state["current_file"],
            "search": _job_state["current_search"],
            "video": _job_state["current_video"],
            "done": _job_state["current_done"],
            "target": _job_state["current_target"],
            "active": bool(_job_state["running"] or _job_state["queue_running"]),
        }


def release_cron_lock():
    base, _, _ = queue_dirs()
    try:
        (base / ".lock").unlink()
    except OSError:
        pass


def process_next_queued():
    """Un passage planifie : traite UNE recherche puis quitte (sans pauses
    longues). Retourne True si quelque chose a ete traite."""
    # Recharge le journal precedent (nouveau processus a chaque passage)
    prev = load_status_file()
    with _job_lock:
        _job_state["logs"] = list(prev.get("logs", []))[-200:]
    acquired, lock_age = acquire_cron_lock()
    if not acquired:
        age_txt = f"{int(lock_age)}s" if lock_age else "?"
        _push_log(f"Passage occupe (attente {age_txt}), abandon.")
        return False
    try:
        task = next_queued_task()
        if task is None:
            _push_log("File vide : rien a faire.")
            with _job_lock:
                _job_state["running"] = False
            _persist_status()
            return False
        fpath, key, query = task
        with _job_lock:
            _job_state["current_file"] = fpath.name
        from indexation.drive import load_state as drive_load_state
        if drive_load_state().get("blocked"):
            _push_log("Drive bloque : passage ignore (bouton Reessayer).")
            with _job_lock:
                _job_state["running"] = False
            _persist_status()
            return False
        _push_log(f"Passage : {key} ({fpath.name}).")
        run_job({key: query})
        if drive_load_state().get("blocked"):
            _push_log("Drive bloque : recherche gardee pour reessai.")
            return False
        mark_search_done(fpath, key)
        _push_log(f"{key} finie et archivee.")
        return True
    finally:
        release_cron_lock()
