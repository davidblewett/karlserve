from __future__ import with_statement

import ConfigParser
import logging
import os
import pkg_resources
import shutil
import sys
import tempfile
import threading
import transaction

from persistent.mapping import PersistentMapping

from repoze.bfg.router import make_app as bfg_make_app
from repoze.depinj import lookup
from repoze.tm import make_tm
from repoze.urchin import UrchinMiddleware
from repoze.who.config import WhoConfig
from repoze.who.plugins.zodb.users import Users
from repoze.who.middleware import PluggableAuthenticationMiddleware
from repoze.zodbconn.finder import PersistentApplicationFinder
from repoze.zodbconn.connector import make_app as zodb_connector
from repoze.zodbconn.uri import db_from_uri

from zope.component import queryUtility

from karlserve.log import configure_log
from karlserve.log import set_subsystem
from karlserve.textindex import KarlPGTextIndex

from karl.bootstrap.interfaces import IBootstrapper
from karl.bootstrap.bootstrap import populate
from karl.errorlog import error_log_middleware
from karl.errorpage import ErrorPageFilter
from karl.models.site import get_weighted_textrepr


def get_instances(settings):
    instances = settings.get('instances')
    if instances is None:
        instances = lookup(Instances)(settings)
        settings['instances'] = instances
    return instances


class Instances(object):

    def __init__(self, settings):
        self.settings = settings
        ini_file = settings['instances_config']
        config = ConfigParser.ConfigParser()
        config.read(ini_file)
        instances = {}
        virtual_hosts = {}
        for section in config.sections():
            if not section.startswith('instance:'):
                continue
            name = section[9:]
            options = dict([(option, config.get(section, option)) for
                            option in config.options(section)])
            instances[name] = LazyInstance(name, settings, options)
            virtual_host = options.get('virtual_host')
            if virtual_host:
                virtual_hosts[virtual_host] = name
        self.instances = instances
        self.virtual_hosts = virtual_hosts

    def get(self, name):
        return self.instances.get(name)

    def get_virtual_host(self, host):
        return self.virtual_hosts.get(host)

    def get_names(self):
        return list(self.instances.keys())

    def close(self):
        for instance in self.instances.values():
            instance.close()


class LazyInstance(object):
    _instance = None
    _pipeline = None
    _tmp_folder = None

    def __init__(self, name, global_config, options):
        self.name = name
        self.options = options

        config = global_config.copy()
        config['blob_cache'] = os.path.join(global_config['blob_cache'], name)
        self.config = config

    def pipeline(self):
        pipeline = self._pipeline
        if pipeline is None:
            instance = self.instance()
            pipeline = lookup(make_karl_pipeline)(instance)
            self._pipeline = pipeline
        return pipeline

    def instance(self):
        instance = self._instance
        if instance is None:
            instance = self._spin_up()
            self._instance = instance
        return instance

    def close(self):
        instance = self._instance
        if instance is not None:
            instance.close()
            self._instance = None

    @property
    def uri(self):
        uri = self.options.get('zodb_uri')
        if uri is None:
            config = self.config
            options = self.options
            uri = self._write_zconfig(
                'zodb.conf', options['dsn'], config['blob_cache'])
            self.options['zodb_uri'] = uri
        return uri

    @property
    def sync_uri(self):
        options = self.options
        uri = options.get('sync.zodb_uri')
        if uri is None:
            dsn = options.get('sync.dsn')
            if dsn is None:
                return None

            blob_cache = os.path.join(
                self.config['sync_folder'], self.name, 'blob_cache')
            uri = self._write_zconfig('sync.conf', dsn, blob_cache)
            options['zodb_uri'] = uri
        return uri

    @property
    def tmp(self):
        tmp = self._tmp_folder
        if tmp is None:
            self._tmp_folder = tmp = tempfile.mkdtemp('.karlserve')
        return tmp

    @apply
    def last_sync_tid():
        def get_fname(self):
            return os.path.join(self.config['sync_folder'], self.name,
                                 'last_sync_tid')

        def __get__(self):
            fname = get_fname(self)
            if not os.path.exists(fname):
                return None
            return int(open(fname).read().strip())

        def __set__(self, tid):
            fname = get_fname(self)
            if tid is None:
                if os.path.exists(fname):
                    os.remove(fname)
            else:
                folder = os.path.dirname(fname)
                if not os.path.exists(folder):
                    os.makedirs(folder)
                with open(fname, 'w') as f:
                    print >> f, str(tid)

        return property(__get__, __set__)

    def _spin_up(self):
        config = self.config
        name = self.name

        # Write postoffice zodb config
        po_uri = config.get('postoffice.zodb_uri')
        if po_uri is None:
            if 'postoffice.dsn' in config:
                if 'postoffice.blob_cache' not in config:
                    raise ValueError("If postoffice.dsn is in config, then "
                                     "postoffice.blob_cache is required.")
                po_uri = self._write_zconfig(
                    'postoffice.conf', config['postoffice.dsn'],
                    config['postoffice.blob_cache'])
                config['postoffice.zodb_uri'] = po_uri
        if po_uri:
            config['postoffice.queue'] = name

        pg_dsn = self.options.get('pgtextindex.dsn')
        if pg_dsn is None:
            pg_dsn = self.options.get('dsn')
        if pg_dsn is not None:
            config['pgtextindex.dsn'] = pg_dsn

        instance = lookup(make_karl_instance)(name, config, self.uri)
        self._instance = instance
        return instance

    def _write_zconfig(self, fname, dsn, blob_cache):
        path = os.path.join(self.tmp, fname)
        uri = 'zconfig://%s' % path
        zconfig = zconfig_template % dict(dsn=dsn, blob_cache=blob_cache)
        with open(path, 'w') as f:
            f.write(zconfig)
        return uri

    def __del__(self):
        if self._tmp_folder is not None:
            shutil.rmtree(self._tmp_folder)


def _get_config(global_config, uri):
    db = db_from_uri(uri)
    conn = db.open()
    root = conn.root()

    config = global_config.copy()
    config.update(hardwired_config)
    instance_config = root.get('instance_config', None)
    if instance_config is None:
        instance_config = PersistentMapping(default_instance_config)
        root['instance_config'] = instance_config
    config.update(instance_config)
    transaction.commit()
    conn.close()
    db.close()
    del db, conn, root

    return config


def make_karl_instance(name, global_config, uri):
    config = _get_config(global_config, uri)

    def appmaker(folder, name='site'):
        if name not in folder:
            bootstrapper = queryUtility(IBootstrapper, default=populate)
            bootstrapper(folder, name)

            # Use pgtextindex
            if 'pgtextindex.dsn' in config:
                site = folder.get(name)
                index = lookup(KarlPGTextIndex)(get_weighted_textrepr)
                site.catalog['texts'] = index

            transaction.commit()

        return folder[name]

    # paster app config callback
    get_root = PersistentApplicationFinder(uri, appmaker)
    def closer():
        db = get_root.db
        if db is not None:
            db.close()
            get_root.db = None

    # Set up logging
    config['get_current_instance'] = get_current_instance
    configure_log(**config)
    set_subsystem('karl')

    # Make BFG app
    pkg_name = config.get('package', None)
    if pkg_name is not None:
        __import__(pkg_name)
        package = sys.modules[pkg_name]
        app = bfg_make_app(get_root, package, options=config)
    else:
        filename = 'karl.includes:standalone.zcml'
        app = bfg_make_app(get_root, filename=filename, options=config)

    app.config = config
    app.uri = uri
    app.close = closer

    return app


def make_who_middleware(app, config):
    who_config = pkg_resources.resource_stream(__name__, 'who.ini').read()
    who_config = who_config % dict(
        cookie=config['who_cookie'],
        secret=config['who_secret'],
        realm=config.get('who_realm', config['system_name']))

    parser = WhoConfig(config['here'])
    parser.parse(who_config)

    return PluggableAuthenticationMiddleware(
        app,
        parser.identifiers,
        parser.authenticators,
        parser.challengers,
        parser.mdproviders,
        parser.request_classifier,
        parser.challenge_decider,
        None,  # log_stream
        logging.INFO,
        parser.remote_user_key,
    )


def make_karl_pipeline(app):
    config = app.config
    uri = app.uri
    pipeline = app
    urchin_account = config.get('urchin.account')
    if urchin_account:
        pipeline = UrchinMiddleware(pipeline, urchin_account)
    pipeline = make_who_middleware(pipeline, config)
    pipeline = make_tm(pipeline, config)
    pipeline = zodb_connector(pipeline, config, zodb_uri=uri)
    pipeline = error_log_middleware(pipeline)
    pipeline = ErrorPageFilter(pipeline, None, 'static', '')
    return pipeline


def find_users(root):
    # Called by repoze.who
    if not 'site' in root:
        return Users()
    return root['site'].users


_threadlocal = threading.local()


def set_current_instance(name):
    _threadlocal.instance = name


def get_current_instance():
    return _threadlocal.instance


default_instance_config = {
    'offline_app_url': 'karl.example.org',
    'system_name': 'KARL',
    'system_email_domain': 'example.org',
    'admin_email': 'admin@example.org',
}

# Hardwired for baby Karls
hardwired_config = {
    'jquery_dev_mode': False,
    'debug': False,
    'reload_templates': False,
    'js_devel_mode': False,
    'static_postfix': '/static',
    'upload_limit': 0,  # XXX ???
    'min_pw_length': 8,
    'selectable_groups': 'group.KarlStaff group.KarlUserAdmin group.KarlAdmin',
    'aspell_executable': 'aspell',
    'aspell_max_words': 5000,
    'aspell_languages': 'en',
}

zconfig_template = """
%%import relstorage
<zodb>
  cache-size 1000
  <relstorage>
    <postgresql>
      dsn %(dsn)s
    </postgresql>
    shared-blob-dir False
    blob-dir %(blob_cache)s
    keep-history false
  </relstorage>
</zodb>
"""
