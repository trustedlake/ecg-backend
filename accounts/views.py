import django.contrib.auth
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)


class LoginView(APIView):
    """POST /api/auth/login/ — email + senha -> {token, user}."""

    authentication_classes = []
    permission_classes = []
    serializer_class = LoginSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]

        # USERNAME_FIELD='email': authenticate recebe username como email.
        user = django.contrib.auth.authenticate(
            request, username=email, password=password
        )
        if user is None:
            raise serializers.ValidationError(
                {"detail": "Email ou senha inválidos."}
            )

        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {
                "token": token.key,
                "user": {
                    "id": str(user.id),
                    "name": user.name,
                    "email": user.email,
                    "crm": user.crm,
                },
            }
        )