from drf_yasg.utils import swagger_auto_schema

from social_django.utils import load_strategy, load_backend
from social_core.exceptions import MissingBackend
from social_core.backends.oauth import BaseOAuth1
from django.http import HttpResponseRedirect
from django.conf import settings
from django.core import mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from rest_framework import status
from rest_framework.generics import RetrieveUpdateAPIView,\
    CreateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from .renderers import UserJSONRenderer
from .serializers import (LoginSerializer, RegistrationSerializer,
                          UserSerializer, SocialAuthenticationSerializer,
                          CreateEmailVerificationSerializer,
                          PasswordChangeSerializer,
                          PasswordResetSerializer, PasswordResetTokenSerializer)
from .utils import validate_image
from authors.apps.core.utils import TokenHandler
from threading import Thread
from .models import User, PasswordResetToken


class RegistrationAPIView(APIView):
    """
    post:
        Register a new user by creating a new user instance.
        All newly registered users will have an email sent
        to their email address for verification
    """
    # Allow any user (authenticated or not) to hit this endpoint.
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)
    serializer_class = RegistrationSerializer

    @swagger_auto_schema(query_serializer=RegistrationSerializer,
                         responses={201: UserSerializer()})
    def post(self, request):
        user = request.data.get('user', {})

        # The create serializer, validate serializer, save serializer pattern
        # below is common and you will see it a lot throughout this course and
        # your own work later on. Get familiar with it.
        serializer = self.serializer_class(data=user)
        serializer.is_valid(raise_exception=True)
        user_email = serializer.validated_data['email']
        username = serializer.validated_data['username']
        callback = {'url': serializer.validated_data['callback_url']}
        token_payload = {'email': user_email,
                         'callback_url': callback['url']}
        domain = settings.DOMAIN
        token = TokenHandler().create_verification_token(token_payload)
        template_name = 'email_verification.html'
        context = {'username': username, 'token': token, 'domain': domain}
        # https://stackoverflow.com/questions/3005080/how-to-send-html-email-with-django-with-dynamic-content-in-it
        html_message = render_to_string(template_name, context)
        text_message = strip_tags(html_message)
        thread = Thread(
            target=mail.send_mail, args=[
                'Please verify your email',
                text_message,
                settings.FROM_EMAIL,
                [user_email, ],
                html_message]
        )
        thread.setDaemon(True)
        thread.start()

        message = {
            'message': 'Successfully created your account. Please proceed to your email ' + # noqa
                   user_email + ' to verify your account.'}
        serializer.save()
        return Response(message, status=status.HTTP_201_CREATED)


class LoginAPIView(APIView):
    """
    post:
        Login an exising user. Users who have not
        verified their accounts should not be
        able to log in.
    """
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)
    serializer_class = LoginSerializer

    @swagger_auto_schema(query_serializer=LoginSerializer,
                         responses={200: UserSerializer()})
    def post(self, request):
        user = request.data.get('user', {})

        # Notice here that we do not call `serializer.save()` like we did for
        # the registration endpoint. This is because we don't actually have
        # anything to save. Instead, the `validate` method on our serializer
        # handles everything we need.
        serializer = self.serializer_class(data=user)
        serializer.is_valid(raise_exception=True)
        username = serializer.data.get('username')
        instance = User.objects.get(username=username)
        user_serializer = UserSerializer(
            instance, context={'current_user': request.user})
        return Response(user_serializer.data, status=status.HTTP_200_OK)


class UserRetrieveUpdateAPIView(RetrieveUpdateAPIView):
    """
    get:
        Retrieve details of a user

    put:
        Update all details of a user

    patch:
        Update a single detail of a user
    """
    permission_classes = (IsAuthenticated,)
    renderer_classes = (UserJSONRenderer,)
    serializer_class = UserSerializer

    def retrieve(self, request, *args, **kwargs):
        # There is nothing to validate or save here. Instead, we just want the
        # serializer to handle turning our `User` object into something that
        # can be JSONified and sent to the client.
        serializer = self.serializer_class(
            request.user, context={'current_user': request.user})

        return Response(serializer.data, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        image = self.request.data.get('image')

        validate_image(image)

        serializer_data = request.data
        user_data = {
            'username': serializer_data.get('username', request.user.username),
            'email': serializer_data.get('email', request.user.email),
            'profile': {
                'first_name': serializer_data.get(
                    'first_name', request.user.profile.last_name),
                'last_name': serializer_data.get(
                    'last_name', request.user.profile.last_name),
                'birth_date': serializer_data.get(
                    'birth_date', request.user.profile.birth_date),
                'bio': serializer_data.get('bio', request.user.profile.bio),
                'image': serializer_data.get(
                    'image', request.user.profile.image),
                'city': serializer_data.get(
                    'city', request.user.profile.city),
                'country': serializer_data.get(
                    'country', request.user.profile.country),
                'phone': serializer_data.get(
                    'phone', request.user.profile.phone),
                'website': serializer_data.get(
                    'website', request.user.profile.website),

            }
        }

        # Here is that serialize, validate, save pattern we talked about
        # before.
        serializer = self.serializer_class(
            request.user, data=user_data, partial=True,
            context={'current_user': request.user}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data, status=status.HTTP_200_OK)


class SocialAuthenticationView(CreateAPIView):
    """
    Login to the site via social authentication
     services (Google, Twitter, Facebook)
    """
    permission_classes = (AllowAny,)
    serializer_class = SocialAuthenticationSerializer
    renderer_classes = (UserJSONRenderer,)

    def create(self, request):
        """Creates user if not present and returns an authentication token"""
        serializer = self.serializer_class(data=request.data)

        serializer.is_valid(raise_exception=True)
        provider = serializer.data.get("provider")
        authenticated_user = request.user if not \
            request.user.is_anonymous else None
        strategy = load_strategy(request)

        # Load backend associated with the provider
        try:

            backend = load_backend(
                strategy=strategy, name=provider, redirect_uri=None)

            access_token = serializer.data.get("access_token")
            if isinstance(backend, BaseOAuth1):
                access_token = {
                    'oauth_token': request.data['access_token'],
                    'oauth_token_secret': request.data['access_token_secret']
                }

        except MissingBackend:
            error_msg = """Provider not supported, Please use 'google-oauth2',
             'facebook', or 'twitter'."""
            return Response({"error": error_msg},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            user = backend.do_auth(access_token, user=authenticated_user)

        except BaseException as error:
            return Response({"error": str(error)},
                            status=status.HTTP_400_BAD_REQUEST)

        user.is_verified = True
        user.save()
        serializer = UserSerializer(
            user, context={'current_user': request.user})
        serializer.instance = user
        return Response(serializer.data, status=status.HTTP_200_OK)


class EmailVerificationView(APIView):
    """We need a view that will handle requests for verifying email adresses"""
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)
    serializer_class = UserSerializer

    def get(self, request, token):
        decoded_token = TokenHandler().validate_token(token)

        if 'email' not in decoded_token:
            return Response(
                {'error': 'invalid token'},
                status=status.HTTP_400_BAD_REQUEST)
        # we check if the user exists and whether they are verified.
        # if we don't find a user we raise an error
        # if we find a registered user, we raise an error
        try:
            user = User.objects.get(email=decoded_token['email'])
        except User.DoesNotExist:
            return Response(
                {'email': 'No user with this email has been registered'},
                status=status.HTTP_404_NOT_FOUND
            )

        if user.is_verified is True:
            return Response(
                {'email': 'This email has already been verified'},
                status=status.HTTP_400_BAD_REQUEST
            )
        user.is_verified = True
        user.save()
        return HttpResponseRedirect(decoded_token['callback_url'])


class CreateEmailVerificationTokenAPIView(APIView):
    """
    This class contains method for creating a new verification
    token for registered users
    """
    permission_classes = (AllowAny,)
    renderer_classes = (UserJSONRenderer,)
    serializer_class = CreateEmailVerificationSerializer

    def post(self, request):
        """This is the method that will be called when users
        want a new verification token."""
        data = request.data

        serializer = self.serializer_class(data=data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.create_payload(data)

        token = TokenHandler().create_verification_token(payload)
        user_email = payload['email']
        domain = settings.DOMAIN
        template_name = 'email_verification.html'
        context = {'username': payload['username'],
                   'token': token, 'domain': domain}
        html_message = render_to_string(template_name, context)
        text_message = strip_tags(html_message)
        thread = Thread(
            target=mail.send_mail, args=[
                'Please verify your email',
                text_message,
                settings.FROM_EMAIL,
                [user_email, ],
                html_message]
        )
        thread.setDaemon(True)
        thread.start()

        message = {'message': 'New verification token created. Please proceed to your email ' + # noqa
                   user_email + ' to verify your account.'}
        return Response(message, status=status.HTTP_201_CREATED)


class PasswordResetView(APIView):
    """
    post:
        Get a user's email where password reset link will be sent.
    """
    def post(self, request):
        data = request.data.get('payload')
        serializer = PasswordResetSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        user_email = data['email']
        callback_url = data['callback_url']
        message = "A password reset link has been sent to your email."
        try:
            user = User.objects.get(email=user_email)
            user_id = user.id
            payload = {

                "email": user_email,
                "callback_url": callback_url
            }
            token = TokenHandler().create_verification_token(payload)
            token_data = {
                "user": user_id,
                "token": token
            }
            serializer = PasswordResetTokenSerializer(data=token_data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            TokenHandler().send_password_reset_link(user_email,
                                                    token, callback_url)
            return Response({"message": message},
                            status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response(
                {"message": message},
                status=status.HTTP_200_OK
            )

    def put(self, request):
        """
         put:
             Update a user's password with a new password.
         """
        try:
            data = request.data.get('user_password')
            serializer = PasswordChangeSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            token = data['token']
            user = PasswordResetToken.objects.get(token=token)
            is_valid = user.is_valid
            if is_valid:
                credentials = TokenHandler().validate_token(token)
                password = data['password']
                confirm_password = data['confirm_password']

                if password != confirm_password:
                    return Response({"message": "Passwords do not Match"})

                serializer = PasswordChangeSerializer(data=data)
                serializer.is_valid(raise_exception=True)
                serializer = PasswordChangeSerializer(instance=User,
                                                      data=data, partial=True)
                serializer = User.objects.get(email=credentials['email'])
                serializer.set_password(password)
                serializer.save()
                user.is_valid = False
                user.save()
                return Response(
                    {'message': 'Your password has been changed.'},
                    status=status.HTTP_202_ACCEPTED)

            else:
                return Response(
                    {'message': 'Sorry, we couldn\'t find that password reset'
                     ' key in our database. Please send another request.'},
                    status=status.HTTP_404_NOT_FOUND
                )

        except PasswordResetToken.DoesNotExist:
            return Response(
                {'message': 'A user with the given token does not exist.'},
                status=status.HTTP_404_NOT_FOUND
            )
