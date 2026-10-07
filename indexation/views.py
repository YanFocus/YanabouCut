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
from indexation.drive import flush_pending_videos
from indexation.drive import load_state as drive_load_state
from indexation.drive import save_state as drive_save_state
from indexation.drive import send_to_drive


def current_json_path():
    return Path(settings.BASE_DIR) / "current_json.json"


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
            # Tentative auto de debloquage : envoyer ce qui attendait
            sent, kept, problem = flush_pending_videos()
            if kept == 0:
                st = drive_load_state()
                st["blocked"] = False
                st["reason"] = ""
                drive_save_state(st)
                messages.success(request, f"Debloque : {sent} video(s) en attente envoyee(s). Vous pouvez relancer un JSON.")
            else:
                messages.error(request, f"Toujours bloque ({problem}). Liberez de l'espace sur Drive.")
            return redirect("home")
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
                # Toujours en file + demarrage auto : tout s'enchaine seul,
                # y compris les JSON deja en attente (pause 1h-2h entre JSON).
                fname = enqueue_searches(searches)
                current_json_path().write_text(json.dumps(searches, ensure_ascii=False, indent=2), encoding="utf-8")
                clear_logs()
                if start_all_queued_async():
                    messages.success(request, f"{len(searches)} recherche(s) : traitement lance, tout s'enchaine seul. Suivez le journal.")
                else:
                    messages.success(request, f"{len(searches)} recherche(s) mise(s) en file ({fname}). Prise en charge automatique a la suite.")
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
    else:
        job = get_job_status()
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
    current_searches = []
    if current_json_path().exists():
        try:
            data = json.loads(current_json_path().read_text(encoding="utf-8"))
            current_searches = list(data.items()) if isinstance(data, dict) else []
        except Exception:
            current_searches = []

    return render(request, "index.html", {
        "json_available": json_available,
        "current_counter": counter,
        "next_video": counter + 1,
        "total_videos_downloaded": total,
        "job": job,
        "job_running": job["running"] or job.get("queue_running", False),
        "job_logs": job["logs"],
        "drive_state": drive_load_state(),
        "pending_videos": [p.name for p in _pending_videos()],
        "queue_mode": queue_mode,
        "queued_files": queued_files,
        "current_searches": current_searches,
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
    sent, kept, problem = flush_pending_videos(
        lambda m: messages.info(request, m))
    st = drive_load_state()
    if kept == 0:
        st["blocked"] = False
        st["reason"] = ""
        drive_save_state(st)
        messages.success(request, f"{sent} video(s) envoyee(s) sur Drive. Chargement des JSON debloque.")
    else:
        if problem == "sature":
            st["reason"] = "Les 4 comptes Google Drive sont satures."
            summary = "Drive sature."
        else:
            st["reason"] = f"Dernier probleme : {problem} (connexion/proxy ?)."
            summary = f"Envoi impossible ({problem}). Verifiez la connexion/proxy."
        st["blocked"] = True
        drive_save_state(st)
        messages.error(request, f"{sent} envoyee(s), {kept} gardee(s) : {summary}")
    return redirect("home")


def _drive_accounts():
    """Comptes connus (mapping) pour les onglets de la page videos."""
    from indexation.drive import load_mapping
    mapping = sorted(load_mapping(), key=lambda m: m["account"])
    return [{"account": m["account"], "email": m.get("email", "")} for m in mapping] or [
        {"account": 1, "email": ""}, {"account": 2, "email": ""},
        {"account": 3, "email": ""}, {"account": 4, "email": ""},
    ]


def videos_page(request, account=1):
    """Liste les videos d'un compte Drive : telechargement + suppression."""
    from indexation.drive import DriveAPI, load_mapping
    try:
        account = int(account)
    except (TypeError, ValueError):
        account = 1
    mapping = {m["account"]: m for m in load_mapping()}
    entry = mapping.get(account)
    videos = []
    error_message = None
    quota_txt = ""
    if entry is None:
        error_message = f"Compte {account} inconnu (mapping Drive absent)."
    else:
        try:
            api = DriveAPI(account)
            for f in api.list_files(entry["folder_id"]):
                size = int(f.get("size") or 0)
                videos.append({
                    "id": f["id"],
                    "name": f.get("name", "?"),
                    "size_mo": round(size / 1024 / 1024, 1),
                    "date": (f.get("modifiedTime", "")[:10]),
                    "download_url": f"https://drive.usercontent.google.com/download?id={f['id']}&export=download&confirm=t",
                })
            try:
                q = api.quota()
                if q["limited"]:
                    quota_txt = f"{q['usage'] // 1024 // 1024} / {q['limit'] // 1024 // 1024} Mo utilises"
                else:
                    quota_txt = "Quota illimite"
            except Exception:
                quota_txt = ""
        except Exception as e:
            error_message = f"Drive injoignable : {str(e)[:200]}"
    return render(request, "videos.html", {
        "accounts": _drive_accounts(),
        "current_account": account,
        "current_email": (entry or {}).get("email", ""),
        "videos": videos,
        "error_message": error_message,
        "quota_txt": quota_txt,
    })


def videos_delete(request, account):
    """Supprime (corbeille) une ou plusieurs videos d'un compte."""
    if request.method != "POST":
        return redirect("videos_account", account=account)
    from indexation.drive import DriveAPI
    ids = request.POST.getlist("file_ids")
    if not ids:
        messages.error(request, "Aucune video selectionnee.")
        return redirect("videos_account", account=account)
    ok, ko = 0, 0
    try:
        api = DriveAPI(int(account))
        for fid in ids:
            try:
                api.trash(fid)
                ok += 1
            except Exception:
                ko += 1
    except Exception as e:
        messages.error(request, f"Drive injoignable : {str(e)[:150]}")
        return redirect("videos_account", account=account)
    if ko:
        messages.error(request, f"{ok} supprimee(s), {ko} echec(s).")
    else:
        messages.success(request, f"{ok} video(s) mise(s) a la corbeille.")
    return redirect("videos_account", account=account)


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
