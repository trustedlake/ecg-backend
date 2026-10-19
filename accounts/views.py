from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import User


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


INVALID_CREDENTIALS = "Usuário ou senha inválidos."


class LoginRateThrottle(AnonRateThrottle):
    """Limita tentativas de login por IP (REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"])."""

    scope = "login"


class LoginView(APIView):
    """POST /api/auth/login/ — username + senha -> {token, user}."""

    authentication_classes = []
    permission_classes = []
    throttle_classes = [LoginRateThrottle]
    serializer_class = LoginSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]

        # Mesma mensagem (e o mesmo custo de hash) para usuário inexistente, senha errada ou
        # usuário inativo: a resposta não revela quais usuários existem.
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
