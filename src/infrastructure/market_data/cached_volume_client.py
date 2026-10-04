class CachedVolumeClient:
    """同一実行内の重複した20日平均取得を再利用します。"""

    def __init__(self, volume_client):
        self.volume_client = volume_client
        self._average_details: dict[tuple[str, int, str | None], dict | Exception] = {}

    def __getattr__(self, name):
        return getattr(self.volume_client, name)

    def get_average_turnover_details(self, symbol, days=20, target_date=None):
        date_key = target_date.isoformat() if target_date is not None else None
        key = (str(symbol), days, date_key)
        saved = self._average_details.get(key)
        if isinstance(saved, Exception):
            raise saved
        if saved is not None:
            return dict(saved)
        try:
            details = self.volume_client.get_average_turnover_details(symbol, days, target_date)
        except Exception as exc:
            self._average_details[key] = exc
            raise
        self._average_details[key] = dict(details)
        return dict(details)