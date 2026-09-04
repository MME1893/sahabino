from uuid import UUID


class ApplicationNotFoundError(Exception):
    def __init__(self, application_id: UUID) -> None:
        super().__init__(f"Application {application_id} was not found")


class DuplicatePackageNameError(Exception):
    def __init__(self, package_name: str) -> None:
        super().__init__(f"An application with package_name '{package_name}' already exists")


class InvalidCategoryAssignmentError(Exception):
    pass
