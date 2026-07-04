import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, field_validator

from app.models.user import UserRole

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Password policy (#13): length-only, NIST-aligned — reject blank/whitespace-only
# and anything outside the min/max length. No character-class complexity
# requirement. The upper bound caps request size on the unauthenticated register
# endpoint (bcrypt truncates at 72 bytes anyway).
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


def validate_password_strength(v: str) -> str:
    """Reject blank/whitespace-only and out-of-range passwords. Returns the value unchanged."""
    if not v or not v.strip():
        raise ValueError("Password must not be blank.")
    if len(v) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(v) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    return v


# Reusable field types so each request model declares the policy inline rather
# than repeating a wrapper validator.
Password = Annotated[str, AfterValidator(validate_password_strength)]
OptionalPassword = Annotated[str | None, AfterValidator(
    lambda v: v if v is None else validate_password_strength(v)
)]


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
    password: Password
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
    password: OptionalPassword = None
