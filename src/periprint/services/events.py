from enum import StrEnum


class EventType(StrEnum):
    SCAN_RESULT = "scan_result"
    SCAN_ERROR = "scan_error"
    CONNECTION_STATUS = "connection_status"
    CONNECTION_ERROR = "connection_error"
    PRINT_PROGRESS = "print_progress"
    PRINT_ERROR = "print_error"
    PRINTER_STATUS = "printer_status"
