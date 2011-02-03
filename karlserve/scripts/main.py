import argparse
import codecs
import logging
import os
import pkg_resources
import sys

from paste.deploy import loadapp
from repoze.bfg.scripting import get_root

from karl.scripting import run_daemon
from karlserve.instance import get_instances

_marker = object


def main(argv=sys.argv, out=None):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    if out is None:
        out = codecs.getwriter('UTF-8')(sys.stdout)

    parser = argparse.ArgumentParser()
    parser.add_argument('-C', '--config', metavar='FILE', default=None,
                        help='Path to configuration ini file.')

    subparsers = parser.add_subparsers(
        title='command', help='Available commands.')
    eps = [ep for ep in pkg_resources.iter_entry_points('karlserve.scripts')]
    eps.sort(key=lambda ep: ep.name)
    ep_names = set()
    for ep in eps:
        if ep.name in ep_names:
            raise RuntimeError('script defined more than once: %s' % ep.name)
        ep_names.add(ep.name)
        ep.load()(ep.name, subparsers, **helpers)

    args = parser.parse_args(argv[1:])
    if args.config is None:
        args.config = get_default_config()

    app = loadapp('config:%s' % args.config, 'karlserve')
    if hasattr(args, 'instance'):
        if not args.instance:
            args.instances = sorted(get_instances(
                app.registry.settings).get_names())
        else:
            args.instances = sorted(args.instance)
        del args.instance

    args.get_instance = instance_factory(args, app)
    args.get_root = instance_root_factory(args, app)
    args.get_setting = settings_factory(args, app)
    args.out = out
    try:
        if getattr(args, 'daemon', False):
            def run():
                args.func(args)
            run_daemon(args.subsystem, run, args.interval)
        else:
            args.func(args)
    finally:
        instances = app.registry.settings.get('instances')
        if instances is not None:
            instances.close()


def get_default_config():
    config = 'karlserve.ini'

    if not os.path.exists(config):
        bin = os.path.abspath(sys.argv[0])
        env = os.path.dirname(os.path.dirname(bin))
        config = os.path.join(env, 'etc', 'karlserve.ini')

    if not os.path.exists(config):
        config = os.path.join('etc', 'karlserve.ini')

    if not os.path.exists(config):
        raise ValueError("Unable to locate config.  Use --config to specify "
                         "path to karlserve.ini")

    return os.path.abspath(config)


def get_instance(app, name):
    instances = get_instances(app.registry.settings)
    return instances.get(name)


def instance_factory(args, app):
    def get(name):
        instance = get_instance(app, name)
        if instance is None:
            args.parser.error("Unknown Karl instance: %s" % name)
        return instance
    return get


def instance_root_factory(args, app):
    def get_instance_root(name):
        instance = get_instance(app, name)
        if instance is None:
            args.parser.error("Unknown Karl instance: %s" % name)
        return get_root(instance.instance())
    return get_instance_root


def settings_factory(args, app):
    settings = app.registry.settings
    def get_setting(name, default=_marker):
        value = settings.get(name, default)
        if value is _marker:
            args.parser.error("Missing setting in configuration: %s" % name)
        return value
    return get_setting


def config_choose_instances(parser):
    parser.add_argument('-I', '--instance', metavar='NAME', action='append',
                        help="Karl instance to use.  May be specifed more "
                        "than once.  If not specified, all Karl instances are "
                        "used.")


def config_daemon_mode(parser, interval=300):
    parser.add_argument('-d', '--daemon', action='store_true',
                        help="Run in daemon mode.")
    parser.add_argument('-i', '--interval', type=int, default=interval,
                        help="Interval in seconds between executions in "
                        "daemon mode.  Default is %d." % interval)

helpers = {
    'config_choose_instances': config_choose_instances,
    'config_daemon_mode': config_daemon_mode,
}
