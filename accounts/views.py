from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import User


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


INVALID_CREDENTIALS = "Invalid username or password."


class LoginRateThrottle(AnonRateThrottle):
    """Limits login attempts per IP (REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"])."""

    scope = "login"


class LoginView(APIView):
    """POST /api/auth/login/: username + password -> {token, user}."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = [LoginRateThrottle]
    serializer_class = LoginSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]

        # Same message (and the same hashing cost) for an unknown user, a wrong password or
        # an inactive user: the response does not reveal which users exist.
        user = User.objects.filter(username__iexact=username).first()
        if user is None:
            User().set_password(password)
            raise serializers.ValidationError({"detail": INVALID_CREDENTIALS})
        if not user.check_password(password) or not user.is_active:
            raise serializers.ValidationError({"detail": INVALID_CREDENTIALS})

        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {
                "token": token.key,
                "user": {
                    "id": str(user.id),
                    "name": user.name,
                    "username": user.username,
                    "email": user.email,
                    "crm": user.crm,
                    "is_staff": user.is_staff,
                    "is_admin": user.is_staff,  # legado (apps/web); o app Expo usa is_staff
                },
            }
        )
