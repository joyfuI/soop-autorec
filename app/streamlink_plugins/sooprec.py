from streamlink.options import Arguments
from streamlink.plugins.soop import Soop


class SoopRecorder(Soop):
    """Use the configured proxy only when issuing HLS AID tokens."""

    # The built-in plugin already registers the legacy --afreeca-* aliases.
    arguments = Arguments(
        *(argument for argument in Soop.arguments if not argument.name.startswith("afreeca-"))
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aid_proxies = dict(self.session.http.proxies)
        self.session.http.proxies.clear()
        self.session.http.trust_env = False

    def _get_hls_key(self, channel, broadcast, quality, pwd):
        # Metadata needs the local IP; AID issuance determines the actual HLS quality.
        self.session.http.proxies.update(self._aid_proxies)
        try:
            return super()._get_hls_key(channel, broadcast, quality, pwd)
        finally:
            self.session.http.proxies.clear()


__plugin__ = SoopRecorder
