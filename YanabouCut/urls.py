from django.contrib import admin
from django.urls import path
from indexation.views import download_site_json, index, job_status_api, retry_drive, run_queued

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', index, name='home'),
    path('telecharger-json/', download_site_json, name='download-json'),
    path('etat-traitement/', job_status_api, name='job-status'),
    path('reessayer-drive/', retry_drive, name='retry-drive'),
    path('lancer-file/', run_queued, name='run-queue'),
]
