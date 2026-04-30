"""Custom JSON encoder for serializing non-standard types."""

import json
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID


class ExtendedJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime, date, time, UUID, and Decimal types."""

    def default(self, o):
        if isinstance(o, (datetime, date, time)):
            return o.isoformat()
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def dumps_with_datetime(obj, **kwargs):
    """Serialize object to JSON string with datetime support."""
    return json.dumps(obj, cls=ExtendedJSONEncoder, **kwargs)
