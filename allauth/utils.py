import random
import re
import string
import unicodedata
import json

from django.core.exceptions import ImproperlyConfigured
from django.core.validators import validate_email, ValidationError
from django.core import urlresolvers
from django.db.models import FieldDoesNotExist
from django.db.models.fields import (DateTimeField, DateField,
                                     EmailField, TimeField)
from django.utils import importlib, six, dateparse
from django.utils.datastructures import SortedDict
from django.core.serializers.json import DjangoJSONEncoder
try:
    from django.utils.encoding import force_text
except ImportError:
    from django.utils.encoding import force_unicode as force_text

from . import app_settings


# Magic number 7: if you run into collisions with this number, then you are
# of big enough scale to start investing in a decent user model...
MAX_USERNAME_SUFFIX_LENGTH = 7
USERNAME_SUFFIX_CHARS = (
    [string.digits] * 4 +
    [string.ascii_letters] * (MAX_USERNAME_SUFFIX_LENGTH - 4))

def _generate_unique_username_base(txts):
    username = None
    for txt in txts:
        username = unicodedata.normalize('NFKD', force_text(txt))
        username = username.encode('ascii', 'ignore').decode('ascii')
        username = force_text(re.sub('[^\w\s@+.-]', '', username).lower())
        # Django allows for '@' in usernames in order to accomodate for
        # project wanting to use e-mail for username. In allauth we don't
        # use this, we already have a proper place for putting e-mail
        # addresses (EmailAddress), so let's not use the full e-mail
        # address and only take the part leading up to the '@'.
        username = username.split('@')[0]
        username = username.strip()
        if username:
            break
    return username or 'user'




def get_username_max_length():
    from .account import app_settings as account_settings
    if account_settings.USER_MODEL_USERNAME_FIELD is not None:
        User = get_user_model()
        max_length = User._meta.get_field(account_settings.USER_MODEL_USERNAME_FIELD).max_length
    else:
        max_length = 0
    return max_length


def generate_username_candidate(basename, suffix_length):
    max_length = get_username_max_length()
    suffix = ''.join(
        random.choice(USERNAME_SUFFIX_CHARS[i])
        for i in range(suffix_length))
    return basename[0:max_length - len(suffix)] + suffix


def generate_username_candidates(basename):
    ret = [basename]
    max_suffix_length = min(
        get_username_max_length(),
        MAX_USERNAME_SUFFIX_LENGTH)
    for suffix_length in range(2, max_suffix_length):
        ret.append(generate_username_candidate(basename, suffix_length))
    return ret


def generate_unique_username(txts):
    from .account import app_settings as account_settings
    from .account.adapter import get_adapter
    adapter = get_adapter()
    basename = _generate_unique_username_base(txts)
    candidates = generate_username_candidates(basename)
    existing_users = set(
        get_user_model()
        .objects
        .filter(**{account_settings.USER_MODEL_USERNAME_FIELD + '__in': candidates})
        .values_list(account_settings.USER_MODEL_USERNAME_FIELD, flat=True))
    for candidate in candidates:
        if candidate not in existing_users:
            try:
                return adapter.clean_username(candidate)
            except ValidationError:
                pass
    # This really should not happen
    raise NotImplementedError('Unable to find a unique username')


def valid_email_or_none(email):
    ret = None
    try:
        if email:
            validate_email(email)
            if len(email) <= EmailField().max_length:
                ret = email
    except ValidationError:
        pass
    return ret


def email_address_exists(email, exclude_user=None):
    from .account import app_settings as account_settings
    from .account.models import EmailAddress

    emailaddresses = EmailAddress.objects
    if exclude_user:
        emailaddresses = emailaddresses.exclude(user=exclude_user)
    ret = emailaddresses.filter(email__iexact=email).exists()
    if not ret:
        email_field = account_settings.USER_MODEL_EMAIL_FIELD
        if email_field:
            users = get_user_model().objects
            if exclude_user:
                users = users.exclude(pk=exclude_user.pk)
            ret = users.filter(**{email_field+'__iexact': email}).exists()
    return ret


def import_attribute(path):
    assert isinstance(path, six.string_types)
    pkg, attr = path.rsplit('.', 1)
    ret = getattr(importlib.import_module(pkg), attr)
    return ret


def import_callable(path_or_callable):
    if not hasattr(path_or_callable, '__call__'):
        ret = import_attribute(path_or_callable)
    else:
        ret = path_or_callable
    return ret


def get_user_model():
    from django.db.models import get_model

    try:
        app_label, model_name = app_settings.USER_MODEL.split('.')
    except ValueError:
        raise ImproperlyConfigured("AUTH_USER_MODEL must be of the"
                                   " form 'app_label.model_name'")
    user_model = get_model(app_label, model_name)
    if user_model is None:
        raise ImproperlyConfigured("AUTH_USER_MODEL refers to model"
                                   " '%s' that has not been installed"
                                   % app_settings.USER_MODEL)
    return user_model


def resolve_url(to):
    """
    Subset of django.shortcuts.resolve_url (that one is 1.5+)
    """
    try:
        return urlresolvers.reverse(to)
    except urlresolvers.NoReverseMatch:
        # If this doesn't "feel" like a URL, re-raise.
        if '/' not in to and '.' not in to:
            raise
    # Finally, fall back and assume it's a URL
    return to


def serialize_instance(instance):
    """
    Since Django 1.6 items added to the session are no longer pickled,
    but JSON encoded by default. We are storing partially complete models
    in the session (user, account, token, ...). We cannot use standard
    Django serialization, as these are models are not "complete" yet.
    Serialization will start complaining about missing relations et al.
    """
    ret = dict([(k, v)
                for k, v in instance.__dict__.items()
                if not k.startswith('_')])
    return json.loads(json.dumps(ret, cls=DjangoJSONEncoder))


def deserialize_instance(model, data):
    ret = model()
    for k, v in data.items():
        if v is not None:
            try:
                f = model._meta.get_field(k)
                if isinstance(f, DateTimeField):
                    v = dateparse.parse_datetime(v)
                elif isinstance(f, TimeField):
                    v = dateparse.parse_time(v)
                elif isinstance(f, DateField):
                    v = dateparse.parse_date(v)
            except FieldDoesNotExist:
                pass
        setattr(ret, k, v)
    return ret


def set_form_field_order(form, fields_order):
    if isinstance(form.fields, SortedDict):
        form.fields.keyOrder = fields_order
    else:
        # Python 2.7+
        from collections import OrderedDict
        assert isinstance(form.fields, OrderedDict)
        form.fields = OrderedDict((f, form.fields[f])
                                  for f in fields_order)
