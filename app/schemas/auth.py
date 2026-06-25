import re

from pydantic import BaseModel, field_validator

from app.models.user import UserRole

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailMixin(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        email = v.strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Enter a valid email address.")
        return email


class RegisterRequest(EmailMixin):
    display_name: str
    password: str
    team: str | None = None
    # NOTE: role is intentionally NOT accepted from the request body. Self-
    # registration always creates a participant; elevation is a privileged,
    # out-of-band/admin operation (#8). Extra fields are ignored by pydantic.


class LoginRequest(EmailMixin):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    role: UserRole
    team: str | None
    actual_role: UserRole | None = None
    actual_team: str | None = None
    can_switch_roles: bool = False

    model_config = {"from_attributes": True}


class UpdateMeRequest(BaseModel):
    display_name: str | None = None
    password: str | None = None
