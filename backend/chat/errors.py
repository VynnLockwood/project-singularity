"""The one error type every chat provider client raises."""


class ChatError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
