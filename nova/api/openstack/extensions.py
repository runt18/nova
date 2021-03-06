# Copyright 2011 OpenStack Foundation
# Copyright 2011 Justin Santa Barbara
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import functools
import os

from oslo_log import log as logging
from oslo_utils import importutils
import six
import webob.dec
import webob.exc

import nova.api.openstack
from nova.api.openstack import wsgi
from nova import exception
from nova.i18n import _
from nova.i18n import _LE
from nova.i18n import _LW
import nova.policy

LOG = logging.getLogger(__name__)


class ExtensionDescriptor(object):
    """Base class that defines the contract for extensions.

    Note that you don't have to derive from this class to have a valid
    extension; it is purely a convenience.

    """

    # The name of the extension, e.g., 'Fox In Socks'
    name = None

    # The alias for the extension, e.g., 'FOXNSOX'
    alias = None

    # Description comes from the docstring for the class

    # The timestamp when the extension was last updated, e.g.,
    # '2011-01-22T19:25:27Z'
    updated = None

    def __init__(self, ext_mgr):
        """Register extension with the extension manager."""

        ext_mgr.register(self)
        self.ext_mgr = ext_mgr

    def get_resources(self):
        """List of extensions.ResourceExtension extension objects.

        Resources define new nouns, and are accessible through URLs.

        """
        resources = []
        return resources

    def get_controller_extensions(self):
        """List of extensions.ControllerExtension extension objects.

        Controller extensions are used to extend existing controllers.
        """
        controller_exts = []
        return controller_exts

    def __repr__(self):
        return "<Extension: name={0!s}, alias={1!s}, updated={2!s}>".format(
            self.name, self.alias, self.updated)

    def is_valid(self):
        """Validate required fields for extensions.

        Raises an attribute error if the attr is not defined
        """
        for attr in ('name', 'alias', 'updated', 'namespace'):
            if getattr(self, attr) is None:
                raise AttributeError("{0!s} is None, needs to be defined".format(attr))
        return True


class ExtensionsController(wsgi.Resource):

    def __init__(self, extension_manager):
        self.extension_manager = extension_manager
        super(ExtensionsController, self).__init__(None)

    def _translate(self, ext):
        ext_data = {}
        ext_data['name'] = ext.name
        ext_data['alias'] = ext.alias
        ext_data['description'] = ext.__doc__
        ext_data['namespace'] = ext.namespace
        ext_data['updated'] = ext.updated
        ext_data['links'] = []  # TODO(dprince): implement extension links
        return ext_data

    def index(self, req):
        extensions = []
        for ext in self.extension_manager.sorted_extensions():
            extensions.append(self._translate(ext))
        return dict(extensions=extensions)

    def show(self, req, id):
        try:
            # NOTE(dprince): the extensions alias is used as the 'id' for show
            ext = self.extension_manager.extensions[id]
        except KeyError:
            raise webob.exc.HTTPNotFound()

        return dict(extension=self._translate(ext))

    def delete(self, req, id):
        raise webob.exc.HTTPNotFound()

    def create(self, req, body):
        raise webob.exc.HTTPNotFound()


class ExtensionManager(object):
    """Load extensions from the configured extension path.

    See nova/tests/api/openstack/compute/extensions/foxinsocks.py or an
    example extension implementation.

    """
    def sorted_extensions(self):
        if self.sorted_ext_list is None:
            self.sorted_ext_list = sorted(six.iteritems(self.extensions))

        for _alias, ext in self.sorted_ext_list:
            yield ext

    def is_loaded(self, alias):
        return alias in self.extensions

    def register(self, ext):
        # Do nothing if the extension doesn't check out
        if not self._check_extension(ext):
            return

        alias = ext.alias
        if alias in self.extensions:
            raise exception.NovaException("Found duplicate extension: {0!s}".format(alias))
        self.extensions[alias] = ext
        self.sorted_ext_list = None

    def get_resources(self):
        """Returns a list of ResourceExtension objects."""

        resources = []
        resources.append(ResourceExtension('extensions',
                                           ExtensionsController(self)))
        for ext in self.sorted_extensions():
            try:
                resources.extend(ext.get_resources())
            except AttributeError:
                # NOTE(dprince): Extension aren't required to have resource
                # extensions
                pass
        return resources

    def get_controller_extensions(self):
        """Returns a list of ControllerExtension objects."""
        controller_exts = []
        for ext in self.sorted_extensions():
            try:
                get_ext_method = ext.get_controller_extensions
            except AttributeError:
                # NOTE(Vek): Extensions aren't required to have
                # controller extensions
                continue
            controller_exts.extend(get_ext_method())
        return controller_exts

    def _check_extension(self, extension):
        """Checks for required methods in extension objects."""
        try:
            extension.is_valid()
        except AttributeError:
            LOG.exception(_LE("Exception loading extension"))
            return False

        return True

    def load_extension(self, ext_factory):
        """Execute an extension factory.

        Loads an extension.  The 'ext_factory' is the name of a
        callable that will be imported and called with one
        argument--the extension manager.  The factory callable is
        expected to call the register() method at least once.
        """

        LOG.debug("Loading extension %s", ext_factory)

        if isinstance(ext_factory, six.string_types):
            if ext_factory.startswith('nova.api.openstack.compute.contrib'):
                LOG.warning(_LW("The legacy v2 API module already moved into"
                             "'nova.api.openstack.compute.legacy_v2.contrib'. "
                             "Use new path instead of old path %s"),
                         ext_factory)
                ext_factory = ext_factory.replace('contrib',
                                                  'legacy_v2.contrib')
            # Load the factory
            factory = importutils.import_class(ext_factory)
        else:
            factory = ext_factory

        # Call it
        LOG.debug("Calling extension factory %s", ext_factory)
        factory(self)

    def _load_extensions(self):
        """Load extensions specified on the command line."""

        extensions = list(self.cls_list)

        for ext_factory in extensions:
            try:
                self.load_extension(ext_factory)
            except Exception as exc:
                LOG.warning(_LW('Failed to load extension %(ext_factory)s: '
                                '%(exc)s'),
                            {'ext_factory': ext_factory, 'exc': exc})


class ControllerExtension(object):
    """Extend core controllers of nova OpenStack API.

    Provide a way to extend existing nova OpenStack API core
    controllers.
    """

    def __init__(self, extension, collection, controller):
        self.extension = extension
        self.collection = collection
        self.controller = controller


class ResourceExtension(object):
    """Add top level resources to the OpenStack API in nova."""

    def __init__(self, collection, controller=None, parent=None,
                 collection_actions=None, member_actions=None,
                 custom_routes_fn=None, inherits=None, member_name=None):
        if not collection_actions:
            collection_actions = {}
        if not member_actions:
            member_actions = {}
        self.collection = collection
        self.controller = controller
        self.parent = parent
        self.collection_actions = collection_actions
        self.member_actions = member_actions
        self.custom_routes_fn = custom_routes_fn
        self.inherits = inherits
        self.member_name = member_name


def load_standard_extensions(ext_mgr, logger, path, package, ext_list=None):
    """Registers all standard API extensions."""

    # Walk through all the modules in our directory...
    our_dir = path[0]
    for dirpath, dirnames, filenames in os.walk(our_dir):
        # Compute the relative package name from the dirpath
        relpath = os.path.relpath(dirpath, our_dir)
        if relpath == '.':
            relpkg = ''
        else:
            relpkg = '.{0!s}'.format('.'.join(relpath.split(os.sep)))

        # Now, consider each file in turn, only considering .py files
        for fname in filenames:
            root, ext = os.path.splitext(fname)

            # Skip __init__ and anything that's not .py
            if ext != '.py' or root == '__init__':
                continue

            # Try loading it
            classname = "{0!s}{1!s}".format(root[0].upper(), root[1:])
            classpath = ("{0!s}{1!s}.{2!s}.{3!s}".format(package, relpkg, root, classname))

            if ext_list is not None and classname not in ext_list:
                logger.debug("Skipping extension: {0!s}".format(classpath))
                continue

            try:
                ext_mgr.load_extension(classpath)
            except Exception as exc:
                logger.warn(_LW('Failed to load extension %(classpath)s: '
                                '%(exc)s'),
                            {'classpath': classpath, 'exc': exc})

        # Now, let's consider any subdirectories we may have...
        subdirs = []
        for dname in dirnames:
            # Skip it if it does not have __init__.py
            if not os.path.exists(os.path.join(dirpath, dname, '__init__.py')):
                continue

            # If it has extension(), delegate...
            ext_name = "{0!s}{1!s}.{2!s}.extension".format(package, relpkg, dname)
            try:
                ext = importutils.import_class(ext_name)
            except ImportError:
                # extension() doesn't exist on it, so we'll explore
                # the directory for ourselves
                subdirs.append(dname)
            else:
                try:
                    ext(ext_mgr)
                except Exception as exc:
                    logger.warn(_LW('Failed to load extension %(ext_name)s:'
                                    '%(exc)s'),
                                {'ext_name': ext_name, 'exc': exc})

        # Update the list of directories we'll explore...
        # using os.walk 'the caller can modify the dirnames list in-place,
        # and walk() will only recurse into the subdirectories whose names
        # remain in dirnames'
        # https://docs.python.org/2/library/os.html#os.walk
        dirnames[:] = subdirs


# This will be deprecated after policy cleanup finished
def core_authorizer(api_name, extension_name):
    def authorize(context, target=None, action=None):
        if target is None:
            target = {'project_id': context.project_id,
                      'user_id': context.user_id}
        if action is None:
            act = '{0!s}:{1!s}'.format(api_name, extension_name)
        else:
            act = '{0!s}:{1!s}:{2!s}'.format(api_name, extension_name, action)
        nova.policy.enforce(context, act, target)
    return authorize


# This is only used for Nova V2 API, after v2 API depreciated, this will be
# deprecated also.
def extension_authorizer(api_name, extension_name):
    return core_authorizer('{0!s}_extension'.format(api_name), extension_name)


def _soft_authorizer(hard_authorizer, api_name, extension_name):
    hard_authorize = hard_authorizer(api_name, extension_name)

    def authorize(context, target=None, action=None):
        try:
            hard_authorize(context, target=target, action=action)
            return True
        except exception.Forbidden:
            return False
    return authorize


# This is only used for Nova V2 API, after V2 API depreciated, this will be
# deprecated also.
def soft_extension_authorizer(api_name, extension_name):
    return _soft_authorizer(extension_authorizer, api_name, extension_name)


# This will be deprecated after policy cleanup finished
def soft_core_authorizer(api_name, extension_name):
    return _soft_authorizer(core_authorizer, api_name, extension_name)


# This will be deprecated after ec2 old style policy removed in later release
def check_compute_policy(context, action, target, scope='compute'):
    _action = '{0!s}:{1!s}'.format(scope, action)
    nova.policy.enforce(context, _action, target)


# NOTE(alex_xu): The functions os_compute_authorizer and
# os_compute_soft_authorizer are used to policy enforcement for OpenStack
# Compute API, now Nova V2.1 REST API will invoke it.
#

def os_compute_authorizer(extension_name):
    return core_authorizer('os_compute_api', extension_name)


def os_compute_soft_authorizer(extension_name):
    return soft_core_authorizer('os_compute_api', extension_name)


@six.add_metaclass(abc.ABCMeta)
class V21APIExtensionBase(object):
    """Abstract base class for all v2.1 API extensions.

    All v2.1 API extensions must derive from this class and implement
    the abstract methods get_resources and get_controller_extensions
    even if they just return an empty list. The extensions must also
    define the abstract properties.
    """

    def __init__(self, extension_info):
        self.extension_info = extension_info

    @abc.abstractmethod
    def get_resources(self):
        """Return a list of resources extensions.

        The extensions should return a list of ResourceExtension
        objects. This list may be empty.
        """
        pass

    @abc.abstractmethod
    def get_controller_extensions(self):
        """Return a list of controller extensions.

        The extensions should return a list of ControllerExtension
        objects. This list may be empty.
        """
        pass

    @abc.abstractproperty
    def name(self):
        """Name of the extension."""
        pass

    @abc.abstractproperty
    def alias(self):
        """Alias for the extension."""
        pass

    @abc.abstractproperty
    def version(self):
        """Version of the extension."""
        pass

    def __repr__(self):
        return "<Extension: name={0!s}, alias={1!s}, version={2!s}>".format(
            self.name, self.alias, self.version)

    def is_valid(self):
        """Validate required fields for extensions.

        Raises an attribute error if the attr is not defined
        """
        for attr in ('name', 'alias', 'version'):
            if getattr(self, attr) is None:
                raise AttributeError("{0!s} is None, needs to be defined".format(attr))
        return True


def expected_errors(errors):
    """Decorator for v2.1 API methods which specifies expected exceptions.

    Specify which exceptions may occur when an API method is called. If an
    unexpected exception occurs then return a 500 instead and ask the user
    of the API to file a bug report.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, webob.exc.WSGIHTTPException):
                    if isinstance(errors, int):
                        t_errors = (errors,)
                    else:
                        t_errors = errors
                    if exc.code in t_errors:
                        raise
                elif isinstance(exc, exception.Forbidden):
                    # Note(cyeoh): Special case to handle
                    # Forbidden exceptions so every
                    # extension method does not need to wrap authorize
                    # calls. ResourceExceptionHandler silently
                    # converts NotAuthorized to HTTPForbidden
                    raise
                elif isinstance(exc, exception.ValidationError):
                    # Note(oomichi): Handle a validation error, which
                    # happens due to invalid API parameters, as an
                    # expected error.
                    raise

                LOG.exception(_LE("Unexpected exception in API method"))
                msg = _('Unexpected API Error. Please report this at '
                    'http://bugs.launchpad.net/nova/ and attach the Nova '
                    'API log if possible.\n%s') % type(exc)
                raise webob.exc.HTTPInternalServerError(explanation=msg)

        return wrapped

    return decorator
