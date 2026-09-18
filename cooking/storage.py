from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateMediaStorage(FileSystemStorage):
    """Filesystem storage without a public URL, served only by authenticated views."""

    @property
    def base_location(self):
        return Path(settings.PRIVATE_MEDIA_ROOT)

    @property
    def location(self):
        return str(self.base_location.resolve())

    @property
    def base_url(self):
        return None


private_media_storage = PrivateMediaStorage()
