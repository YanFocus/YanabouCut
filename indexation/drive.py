"""Envoi des videos vers Google Drive (multi-comptes avec rotation).

Principe :
- 4 comptes Google autorises une fois via OAuth (tokens dans drive_config/tokens/).
- Chaque video est envoyee au premier compte ayant assez de quota (marge 100 Mo).
- Ordre de rotation 1 -> 2 -> 3 -> 4, reprise apres le dernier compte utilise.
- Si aucun compte n'a la place : la video reste en local + flag "blocked".
"""
import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
SAFETY_MARGIN_BYTES = 100 * 1024 * 1024  # marge de securite exigee en plus du fichier


def config_dir():
    from django.conf import settings
    d = Path(getattr(settings, "DRIVE_CONFIG_DIR"))
    (d / "secrets").mkdir(parents=True, exist_ok=True)
    (d / "tokens").mkdir(parents=True, exist_ok=True)
    return d


def secret_files():
    return sorted((config_dir() / "secrets").glob("*.json"))


def secret_path(account):
    files = secret_files()
    if not (1 <= account <= len(files)):
        raise ValueError(f"Compte {account} invalide (1-{len(files)})")
    return files[account - 1]


def token_path(account):
    return config_dir() / "tokens" / f"token_{account}.json"


def load_folders():
    with open(config_dir() / "folders.json", encoding="utf-8") as f:
        return json.load(f)


def mapping_path():
    return config_dir() / "drive_mapping.json"


def load_mapping():
    p = mapping_path()
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def save_mapping(mapping):
    mapping_path().write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def state_path():
    return config_dir() / "drive_state.json"


def load_state():
    p = state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"blocked": False, "last_account": 0, "reason": ""}


def save_state(state):
    state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_flow(account):
    return InstalledAppFlow.from_client_secrets_file(
        str(secret_path(account)), SCOPES, redirect_uri="http://localhost"
    )


def _verifier_path(account):
    return config_dir() / f".verifier_{account}"


def auth_url(account):
    flow = build_flow(account)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    # Persiste le code_verifier PKCE : l'echange du code se fera dans un
    # autre processus, qui doit presenter le meme verifier.
    _verifier_path(account).write_text(flow.code_verifier or "", encoding="utf-8")
    return url


def finish_auth(account, code):
    flow = build_flow(account)
    vp = _verifier_path(account)
    if vp.exists():
        flow.code_verifier = vp.read_text(encoding="utf-8").strip() or None
    flow.fetch_token(code=code.strip())
    try:
        vp.unlink()
    except OSError:
        pass
    token_path(account).write_text(flow.credentials.to_json(), encoding="utf-8")
    return flow.credentials


def get_service(account):
    creds = Credentials.from_authorized_user_file(str(token_path(account)), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path(account).write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)


def account_email(service):
    about = service.about().get(fields="user(emailAddress)").execute()
    return about.get("user", {}).get("emailAddress", "?")


def quota_info(service):
    q = service.about().get(fields="storageQuota").execute().get("storageQuota", {})
    limit = q.get("limit")
    usage = int(q.get("usage") or 0)
    if limit is None:
        return {"limited": False, "free": None, "usage": usage, "limit": None}
    limit = int(limit)
    return {"limited": True, "free": limit - usage, "usage": usage, "limit": limit}


def check_folder(service, folder_id):
    try:
        meta = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType,capabilities",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        return {"ok": False, "error": str(e)[:200]}
    caps = meta.get("capabilities", {})
    return {"ok": True, "name": meta.get("name"), "writable": bool(caps.get("canAddChildren"))}


def upload_video(service, folder_id, local_path, drive_name):
    media = MediaFileUpload(str(local_path), mimetype="video/mp4", resumable=True)
    body = {"name": drive_name, "parents": [folder_id]}
    return service.files().create(
        body=body, media_body=media, fields="id,name,size", supportsAllDrives=True
    ).execute()


def find_in_folder(service, folder_id, name):
    """Retourne l'ID du fichier s'il existe deja dans le dossier, sinon None."""
    safe = name.replace("'", "\\'")
    res = service.files().list(
        q=f"name='{safe}' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        supportsAllDrives=True,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def backup_site_json(log=None):
    """Sauvegarde json_du_site.json dans le dossier prevu (compte 1).
    Mise a jour si deja present, creation sinon. Ne leve jamais (log only).
    Desactivee si YANABOU_NO_BACKUP=1 (tests)."""
    import os
    if os.environ.get("YANABOU_NO_BACKUP"):
        return None
    from django.conf import settings
    site_json = Path(getattr(settings, "SITE_JSON_PATH"))
    folder_id = getattr(settings, "DRIVE_BACKUP_FOLDER_ID", "")
    if not site_json.exists() or not folder_id:
        return None
    try:
        service = get_service(1)
        media = MediaFileUpload(str(site_json), mimetype="application/json", resumable=True)
        existing = find_in_folder(service, folder_id, site_json.name)
        if existing:
            updated = service.files().update(
                fileId=existing, media_body=media, fields="id,modifiedTime",
                supportsAllDrives=True).execute()
            if log:
                log(f"[Backup] json_du_site.json mis a jour sur Drive (compte 1).")
            return updated.get("id")
        created = service.files().create(
            body={"name": site_json.name, "parents": [folder_id]},
            media_body=media, fields="id", supportsAllDrives=True).execute()
        if log:
            log(f"[Backup] json_du_site.json sauvegarde sur Drive (compte 1).")
        return created.get("id")
    except Exception as e:
        if log:
            log(f"[Backup] ECHEC sauvegarde JSON : {str(e)[:150]} (le traitement continue).", level="error")
        return None


def send_to_drive(local_path, drive_name, log):
    """Envoie une video sur le compte prioritaire ayant la place.

    Ordre strict : compte 1, puis 2, 3, 4. Un compte inferieur n'est utilise
    que si tous les comptes au-dessus sont pleins. A chaque video, on
    re-verifie depuis le compte 1 (une place liberee est donc reutilisee).
    Si le fichier existe deja dans le dossier, succes immediat (anti-doublon).
    Retourne (numero_compte, file_id) ou (None, "sature"/"erreur"/"config...").
    """
    mapping = load_mapping()
    if not mapping:
        return None, "config: mapping Drive absent (lancez : python manage.py drive_probe)"
    mapping = sorted(mapping, key=lambda m: m["account"])
    state = load_state()
    size = Path(local_path).stat().st_size
    tried = 0
    quota_blocks = 0
    for entry in mapping:
        account = entry["account"]
        try:
            service = get_service(account)
        except Exception as e:
            log(f"[Drive] Compte {account} : ERREUR authentification ({str(e)[:120]}) -> suivant.")
            tried += 1
            continue
        already = find_in_folder(service, entry["folder_id"], drive_name)
        if already:
            log(f"[Drive] {drive_name} deja present sur le compte {account} (anti-doublon).")
            state["last_account"] = account
            state["blocked"] = False
            state["reason"] = ""
            save_state(state)
            return account, already
        q = quota_info(service)
        if q["limited"] and q["free"] < size + SAFETY_MARGIN_BYTES:
            log(f"[Drive] Compte {account} ({entry.get('email')}) sature "
                f"(libre {q['free'] // 1024 // 1024} Mo) -> suivant.")
            tried += 1
            quota_blocks += 1
            continue
        try:
            created = upload_video(service, entry["folder_id"], local_path, drive_name)
        except HttpError as e:
            if "storageQuotaExceeded" in str(e) or "quotaExceeded" in str(e):
                log(f"[Drive] Compte {account} sature pendant l'upload -> suivant.")
                tried += 1
                quota_blocks += 1
                continue
            raise
        state["last_account"] = account
        state["blocked"] = False
        state["reason"] = ""
        save_state(state)
        return account, created.get("id")
    if tried > 0 and quota_blocks == tried:
        return None, "sature"
    return None, "erreur"
