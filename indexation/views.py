import json
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render

from .services import (
    clear_logs,
    enqueue_searches,
    get_job_status,
    is_running,
    load_queue_progress,
    load_site_json,
    load_status_file,
    queue_dirs,
    start_all_queued_async,
    start_job_async,
    start_next_queued_async,
)
from indexation.drive import load_state as drive_load_state
from indexation.drive import save_state as drive_save_state
from indexation.drive import send_to_drive


def _pending_videos():
    videos_dir = Path(settings.VIDEOS_DIR)
    if not videos_dir.exists():
        return []
    return sorted(videos_dir.glob("video_*.mp4"), key=lambda p: p.name)


def index(request):
    # POST : traite l'upload puis redirige (Post/Redirect/Get) pour que les
    # rechargements auto du journal (GET) ne re-envoient jamais le fichier,
    # ce qui relancait un nouveau traitement en boucle.
    if request.method == "POST":
        drive_state = drive_load_state()
        if drive_state.get("blocked"):
            messages.error(request, f"Chargement bloque ({drive_state.get('reason', 'envoi Drive impossible')}). "
                                    "Cliquez sur « Reessayer l'envoi Drive » apres correction.")
        elif settings.PIPELINE_MODE == "queue":
            # Mode heberge : mise en file, traitement par tache planifiee
            json_file = request.FILES.get("json_file")
            if not json_file:
                messages.error(request, "Veuillez selectionner un fichier JSON.")
            else:
                try:
                    raw = json_file.read().decode("utf-8")
                    data = json.loads(raw)
                    if not isinstance(data, dict) or not data:
                        raise ValueError("Le JSON doit etre un objet non vide, ex : {\"recherche1\": \"dog in house\"}")
                    searches = {str(k): str(v) for k, v in data.items() if str(v).strip()}
                    if not searches:
                        raise ValueError("Aucune requete valide trouvee dans le JSON.")
                    fname = enqueue_searches(searches)
                    clear_logs()
                    messages.success(request, f"{len(searches)} recherche(s) mise(s) en file ({fname}). "
                                              "Journal reinitialise : il affichera ce JSON. "
                                              "La tache planifiee les traitera une par une.")
                except Exception as e:
                    messages.error(request, f"Erreur : {e}")
        elif is_running():
            messages.error(request, "Un traitement est deja en cours. Attendez la fin avant de relancer.")
        else:
            json_file = request.FILES.get("json_file")
            if not json_file:
                messages.error(request, "Veuillez selectionner un fichier JSON.")
            else:
                try:
                    raw = json_file.read().decode("utf-8")
                    data = json.loads(raw)
                    if not isinstance(data, dict) or not data:
                        raise ValueError("Le JSON doit etre un objet non vide, ex : {\"recherche1\": \"dog in house\"}")
                    searches = {str(k): str(v) for k, v in data.items() if str(v).strip()}
                    if not searches:
                        raise ValueError("Aucune requete valide trouvee dans le JSON.")
                    ok = start_job_async(searches)
                    if ok:
                        clear_logs()
                        messages.success(request, f"Traitement demarre pour {len(searches)} recherche(s) : {', '.join(searches.keys())}. Journal reinitialise : il affichera ce JSON.")
                    else:
                        messages.error(request, "Un traitement est deja en cours.")
                except Exception as e:
                    messages.error(request, f"Erreur : {e}")
        return redirect("home")

    site_json_path = Path(settings.SITE_JSON_PATH)
    state = load_site_json()
    counter = int(state.get("counter", 0))
    total = len(state.get("videos", []))
    json_available = site_json_path.exists()
    queue_mode = settings.PIPELINE_MODE == "queue"
    if queue_mode:
        # Le processus planifie est separe : on lit son journal via le fichier
        job = load_status_file()
        _, pending_dir, _ = queue_dirs()
        progress = load_queue_progress()
        queued_files = []
        for fpath in sorted(pending_dir.glob("*.json")):
            try:
                searches = json.loads(fpath.read_text(encoding="utf-8")).get("searches", {})
            except Exception:
                searches = {}
            done_keys = progress.get(fpath.name, [])
            queued_files.append({
                "name": fpath.name,
                "total": len(searches),
                "done": len([k for k in searches if k in done_keys]),
            })
    else:
        job = get_job_status()
        queued_files = []
    last_videos = state.get("videos", [])[-5:][::-1]

    return render(request, "index.html", {
        "json_available": json_available,
        "current_counter": counter,
        "next_video": counter + 1,
        "total_videos_downloaded": total,
        "last_videos": last_videos,
        "job": job,
        "job_running": job["running"] or job.get("queue_running", False),
        "job_logs": job["logs"],
        "drive_state": drive_load_state(),
        "pending_videos": [p.name for p in _pending_videos()],
        "queue_mode": queue_mode,
        "queued_files": queued_files,
    })


def run_queued(request):
    """Bouton web : lance le traitement AUTOMATIQUE de tout le JSON en file."""
    if request.method != "POST":
        return redirect("home")
    drive_state = drive_load_state()
    if drive_state.get("blocked"):
        messages.error(request, f"Traitement bloque ({drive_state.get('reason', 'envoi Drive impossible')}). "
                                "Cliquez sur « Reessayer l'envoi Drive » apres correction.")
    elif not next_task_available():
        messages.error(request, "File d'attente vide : uploadez d'abord un JSON.")
    elif not start_all_queued_async():
        messages.error(request, "Un traitement est deja en cours, attendez sa fin.")
    else:
        messages.success(request, "Traitement automatique lance : toutes les recherches vont s'enchainer. Suivez le journal.")
    return redirect("home")


def next_task_available():
    from .services import next_queued_task
    return next_queued_task() is not None


def retry_drive(request):
    """Reessaie l'envoi des videos gardees en local. Debloque si ca passe."""
    if request.method != "POST":
        return redirect("home")
    if is_running():
        messages.error(request, "Un traitement est en cours, reessayez apres sa fin.")
        return redirect("home")
    pending = _pending_videos()
    if not pending:
        st = drive_load_state()
        st["blocked"] = False
        st["reason"] = ""
        drive_save_state(st)
        messages.success(request, "Aucune video en attente. Chargement des JSON debloque.")
        return redirect("home")
    sent, kept = 0, 0
    last_problem = ""
    for local_path in pending:
        try:
            account_used, result = send_to_drive(local_path, local_path.name, lambda m: None)
        except Exception as e:
            messages.error(request, f"Erreur Drive pour {local_path.name} : {str(e)[:150]}")
            last_problem = str(e)[:150]
            kept += 1
            continue
        if account_used:
            try:
                local_path.unlink()
                sent += 1
            except OSError:
                sent += 1
        else:
            last_problem = str(result)
            kept += 1
    st = drive_load_state()
    if kept == 0:
        st["blocked"] = False
        st["reason"] = ""
        drive_save_state(st)
        messages.success(request, f"{sent} video(s) envoyee(s) sur Drive. Chargement des JSON debloque.")
    else:
        if last_problem == "sature":
            st["reason"] = "Les 4 comptes Google Drive sont satures."
            summary = "Drive sature."
        else:
            st["reason"] = f"Dernier probleme : {last_problem} (connexion/proxy ?)."
            summary = f"Envoi impossible ({last_problem}). Verifiez la connexion/proxy."
        st["blocked"] = True
        drive_save_state(st)
        messages.error(request, f"{sent} envoyee(s), {kept} gardee(s) : {summary}")
    return redirect("home")


def download_site_json(request):
    site_json_path = Path(settings.SITE_JSON_PATH)
    if not site_json_path.exists():
        raise Http404("json_du_site.json n'existe pas encore. Lancez d'abord un traitement.")
    return FileResponse(
        open(site_json_path, "rb"),
        as_attachment=True,
        filename="json_du_site.json",
        content_type="application/json",
    )


def job_status_api(request):
    state = load_site_json()
    job = get_job_status()
    return JsonResponse({
        "running": job["running"],
        "queue_running": job.get("queue_running", False),
        "current_search": job["current_search"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "logs": job["logs"],
        "counter": int(state.get("counter", 0)),
        "total": len(state.get("videos", [])),
        "json_available": Path(settings.SITE_JSON_PATH).exists(),
    })
