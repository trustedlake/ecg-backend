from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import User


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class LoginView(APIView):
    """POST /api/auth/login/ — username + senha -> {token, user}."""

    authentication_classes = []
    permission_classes = []
    serializer_class = LoginSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise serializers.ValidationError({"detail": "Usuário inválido."})

        if not user.check_password(password):
            raise serializers.ValidationError({"detail": "Senha inválida."})
        if not user.is_active:
            raise serializers.ValidationError({"detail": "Usuário inválido."})

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
                    "is_admin": user.is_staff,
                },
            }
        )
