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


class AdminCreateUserRequest(EmailMixin):
    """Admin-provisioned account (#67). Unlike self-registration this may set any
    role and the admin flag — it is the out-of-band path used when
    REGISTRATION_ENABLED is off. Guarded by require_admin, never by the register
    route, so accepting role/is_admin here is not self-elevation (#8)."""

    display_name: str
    password: Password
    role: UserRole = UserRole.participant
    team: str | None = None
    is_admin: bool = False


class AdminResetPasswordRequest(BaseModel):
    """Admin-driven password reset (#66). The admin supplies the new (temporary)
    password; must_change_password defaults on so the user is prompted to set their
    own on next login. Guarded by require_admin."""

    password: Password
    must_change_password: bool = True


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
    is_admin: bool = False
    must_change_password: bool = False
    actual_role: UserRole | None = None
    actual_team: str | None = None
    can_switch_roles: bool = False

    model_config = {"from_attributes": True}


class UpdateMeRequest(BaseModel):
    display_name: str | None = None
    password: OptionalPassword = None
