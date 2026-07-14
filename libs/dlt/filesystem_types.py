from typing import Protocol


class WritableFile(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def string(self) -> str: ...
