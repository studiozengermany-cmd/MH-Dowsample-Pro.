"""Typed failures used across Audio Organizer."""


class AudioOrganizerError(Exception):
    """Base application error."""


class ConfigError(AudioOrganizerError):
    pass


class QualityGateError(AudioOrganizerError):
    pass


class AudioAnalysisError(QualityGateError):
    pass


class ProcessorError(AudioOrganizerError):
    pass


class ConversionError(ProcessorError):
    pass


class TaggingError(ProcessorError):
    pass


class OrganizerError(AudioOrganizerError):
    pass


class DuplicateFileError(OrganizerError):
    pass


class DatabaseError(OrganizerError):
    pass


class CrawlerError(AudioOrganizerError):
    pass


class NetworkError(CrawlerError):
    pass


class CrawlTimeoutError(CrawlerError):
    pass


class BrowserUnavailableError(CrawlerError):
    pass


class NoAudioFoundError(CrawlerError):
    pass


class CrawlLimitError(CrawlerError):
    pass


class HTTPError(NetworkError):
    def __init__(self, status_code: int, message: str = "HTTP request failed") -> None:
        super().__init__(f"{message}: {status_code}")
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.status_code == 429 or self.status_code >= 500


class PathTraversalError(CrawlerError):
    pass


class FileTooLargeError(CrawlerError):
    pass
