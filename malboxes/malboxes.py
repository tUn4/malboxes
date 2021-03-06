# Malboxes - Vagrant box builder and config generator for malware analysis
# https://github.com/gosecure/malboxes
#
# Olivier Bilodeau <obilodeau@gosecure.ca>
# Hugo Genesse <hugo.genesse@polymtl.ca>
# Copyright (C) 2016 GoSecure Inc.
# Copyright (C) 2016 Hugo Genesse
# All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
import argparse
import glob
from io import TextIOWrapper
import json
import os
from pkg_resources import resource_filename, resource_stream
import re
import shutil
import signal
import subprocess
import sys
import textwrap

from appdirs import AppDirs
from jinja2 import Environment, FileSystemLoader
from jsmin import jsmin

from malboxes._version import __version__

DIRS = AppDirs("malboxes")
DEBUG = False

def initialize():
    # create appdata directories if they don't exist
    if not os.path.exists(DIRS.user_config_dir):
        os.makedirs(DIRS.user_config_dir)

    if not os.path.exists(DIRS.user_cache_dir):
        os.makedirs(DIRS.user_cache_dir)

    return init_parser()

def init_parser():
    parser = argparse.ArgumentParser(
                    description="Vagrant box builder "
                                "and config generator for malware analysis.")
    parser.add_argument('-V', '--version', action='version',
                        version='%(prog)s ' + __version__)
    parser.add_argument('-d', '--debug', action='store_true', help="Debug mode")
    subparsers = parser.add_subparsers()

    # list command
    parser_list = subparsers.add_parser('list',
                                        help="Lists available profiles.")
    parser_list.set_defaults(func=list_profiles)

    # build command
    parser_build = subparsers.add_parser('build',
                                         help="Builds a Vagrant box based on "
                                              "a given profile.")
    parser_build.add_argument('profile', help='Name of the profile to build. '
                                              'Use list command to view '
                                              'available profiles.')
    parser_build.add_argument('--skip-packer-build', action='store_true',
                              help='Skip packer build phase. '
                                   'Only useful for debugging.')
    parser_build.add_argument('--skip-vagrant-box-add', action='store_true',
                              help='Skip vagrant box add phase. '
                                   'Only useful for debugging.')
    parser_build.set_defaults(func=build)

    # spin command
    parser_spin = subparsers.add_parser('spin',
                                        help="Creates a Vagrantfile for "
                                             "your profile / Vagrant box.")
    parser_spin.add_argument('profile', help='Name of the profile to spin.')
    parser_spin.add_argument('name', help='Name of the target VM. '
                                          'Must be unique on your system. '
                                          'Ex: Cryptolocker_XYZ.')
    parser_spin.set_defaults(func=spin)

    # reg command
    parser_reg = subparsers.add_parser('registry',
                                       help='Modifies a registry key.')
    parser_reg.add_argument('profile', help='Name of the profile to modify.')
    parser_reg.add_argument('modtype',
                            help='Modification type (add, delete or modify).')
    parser_reg.add_argument('key', help='Location of the key to modify.')
    parser_reg.add_argument('name', help='Name of the key.')
    parser_reg.add_argument('value', help='Value of the key.')
    parser_reg.add_argument('valuetype',
                            help='Type of the value of the key: '
                                 'DWORD for integer, String for string.')
    parser_reg.set_defaults(func=reg)

    # dir command
    parser_dir = subparsers.add_parser('directory',
                                       help='Modifies a directory.')
    parser_dir.add_argument('profile',
                            help='Name of the profile to modify.')
    parser_dir.add_argument('modtype',
                            help='Modification type (delete or add).')
    parser_dir.add_argument('dirpath',
                            help='Path of the directory to modify.')
    parser_dir.set_defaults(func=directory)

    # wallpaper command

    # package command
    parser_package = subparsers.add_parser('package',
                                           help='Adds package to install.')
    parser_package.add_argument('profile',
                                help='Name of the profile to modify.')
    parser_package.add_argument('package',
                                help='Name of the package to install.')
    parser_package.set_defaults(func=package)

    # document command
    parser_document = subparsers.add_parser('document',
                                            help='Adds a file')
    parser_document.add_argument('profile',
                                 help='Name of the profile to modify.')
    parser_document.add_argument('modtype',
                                 help='Modification type (delete or add).')
    parser_document.add_argument('docpath',
                                 help='Path of the file to add.')
    parser_document.set_defaults(func=document)

    # no command
    parser.set_defaults(func=default)

    args = parser.parse_args()
    return parser, args


def prepare_autounattend(config):
    """
    Prepares an Autounattend.xml file according to configuration and writes it
    into a temporary location where packer later expects it.

    Uses jinja2 template syntax to generate the resulting XML file.
    """
    os_type = _get_os_type(config)

    filepath = resource_filename(__name__, "installconfig/")
    env = Environment(loader=FileSystemLoader(filepath))
    template = env.get_template("{}/Autounattend.xml".format(os_type))
    f = create_cachefd('Autounattend.xml')
    f.write(template.render(config)) # pylint: disable=no-member
    f.close()


def prepare_packer_template(config, template_name):
    """
    Prepares a packer template JSON file according to configuration and writes
    it into a temporary location where packer later expects it.

    Uses jinja2 template syntax to generate the resulting JSON file.
    Templates are in profiles/ and snippets in profiles/snippets/.
    """
    try:
        profile_fd = resource_stream(__name__,
                                     'profiles/{}.json'.format(template_name))
    except FileNotFoundError:
        print("Profile doesn't exist: {}".format(template_name))
        sys.exit(2)

    filepath = resource_filename(__name__, 'profiles/')
    env = Environment(loader=FileSystemLoader(filepath), autoescape=False,
                      trim_blocks=True, lstrip_blocks=True)
    template = env.get_template("{}.json".format(template_name))

    # write to temporary file
    f = create_cachefd('{}.json'.format(template_name))
    f.write(template.render(config)) # pylint: disable=no-member
    f.close()
    return f.name


def _prepare_vagrantfile(config, source, fd_dest):
    """
    Creates Vagrantfile based on a template using the jinja2 engine. Used for
    spin and also for the packer box Vagrantfile. Based on templates in
    vagrantfiles/.
    """
    filepath = resource_filename(__name__, "vagrantfiles/")
    env = Environment(loader=FileSystemLoader(filepath))
    template = env.get_template(source)

    fd_dest.write(template.render(config)) # pylint: disable=no-member
    fd_dest.close()


def prepare_config(profile):
    """
    Prepares Malboxes configuration and merge with Packer profile configuration

    Packer uses a configuration in JSON so we decided to go with JSON as well.
    However since we have features that should be easily "toggled" by our users
    I wanted to add an easy way of "commenting out" or "uncommenting" a
    particular feature. JSON doesn't support comments. However JSON's author
    gives a nice suggestion here[1] that I will follow.

    In a nutshell, our configuration is Javascript, which when minified gives
    JSON and then it gets merged with the selected profile.

    [1]: https://plus.google.com/+DouglasCrockfordEsq/posts/RK8qyGVaGSr
    """
    # if config does not exist, copy default one
    config_file = os.path.join(DIRS.user_config_dir, 'config.js')
    if not os.path.isfile(config_file):
        print("Default configuration doesn't exist. Populating one: {}"
              .format(config_file))
        shutil.copy(resource_filename(__name__, 'config-example.js'),
                    config_file)

    config = load_config(config_file, profile)

    packer_tmpl = prepare_packer_template(config, profile)

    # merge/update with profile config
    with open(packer_tmpl, 'r') as f:
        config.update(json.loads(f.read()))

    return config, packer_tmpl


def load_config(config_file, profile):
    """Loads the minified JSON config. Returns a dict."""
    config = {}
    with open(config_file, 'r') as f:
        # minify then load as JSON
        config = json.loads(jsmin(f.read()))

    # add packer required variables
    # Note: Backslashes are replaced with forward slashes (Packer on Windows)
    config['cache_dir'] = DIRS.user_cache_dir.replace('\\', '/')
    config['dir'] = resource_filename(__name__, "").replace('\\', '/')
    config['profile_name'] = profile
    return config


def _get_os_type(config):
    """OS Type is extracted from profile json config"""
    return config['builders'][0]['guest_os_type'].lower()


tempfiles = []
def create_cachefd(filename):
    tempfiles.append(filename)
    return open(os.path.join(DIRS.user_cache_dir, filename), 'w')


def cleanup():
    """Removes temporary files. Keep them in debug mode."""
    if not DEBUG:
        for f in tempfiles:
            os.remove(os.path.join(DIRS.user_cache_dir, f))


def run_foreground(command):
    if DEBUG:
        print("DEBUG: Executing {}".format(command))
    p = subprocess.Popen(command, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    try:
        for line in iter(p.stdout.readline, b''):
            print(line.rstrip().decode('utf-8'))

    # send Ctrl-C to subprocess
    except KeyboardInterrupt:
        p.send_signal(signal.SIGINT)
        for line in iter(p.stdout.readline, b''):
            print(line.rstrip().decode('utf-8'))
        raise

    finally:
        p.wait()
        return p.returncode


def run_packer(packer_tmpl, args):
    print("Starting packer to generate the VM")
    print("----------------------------------")

    prev_cwd = os.getcwd()
    os.chdir(DIRS.user_cache_dir)

    try:
        # packer or packer-io?
        binary = 'packer'
        if shutil.which(binary) == None:
            binary = 'packer-io'
            if shutil.which(binary) == None:
                print("packer not found. Install it: "
                      "https://www.packer.io/intro/getting-started/setup.html")
                return 254

        # run packer with relevant config minified
        configfile = os.path.join(DIRS.user_config_dir, 'config.js')
        with open(configfile, 'r') as config:
            f = create_cachefd('packer_var_file.json')
            f.write(jsmin(config.read()))
            f.close()

        flags = ['-var-file={}'.format(f.name)]
        if DEBUG:
            flags.append('-debug')

        cmd = [binary, 'build']
        cmd.extend(flags)
        cmd.append(packer_tmpl)
        ret = run_foreground(cmd)

    finally:
        os.chdir(prev_cwd)

    print("----------------------------------")
    print("packer completed with return code: {}".format(ret))
    return ret


def add_box(config, args):
    print("Adding box into vagrant")
    print("--------------------------")

    box = config['post-processors'][0]['output']
    box = os.path.join(DIRS.user_cache_dir, box)
    box = box.replace('{{user `name`}}', args.profile)

    cmd = ['vagrant', 'box', 'add', box, '--name={}'.format(args.profile)]
    ret = run_foreground(cmd)

    print("----------------------------")
    print("vagrant box add completed with return code: {}".format(ret))
    return ret


def default(parser, args):
    parser.print_help()
    print("\n")
    list_profiles(parser, args)
    sys.exit(1)


def list_profiles(parser, args):
    print("supported profiles:\n")

    filepath = resource_filename(__name__, "profiles/")
    for f in sorted(glob.glob(os.path.join(filepath, '*.json'))):
        m = re.search(r'profiles[\/\\](.*).json$', f)
        print(m.group(1))
    print()


def build(parser, args):

    print("Generating configuration files...")
    config, packer_tmpl = prepare_config(args.profile)
    prepare_autounattend(config)
    _prepare_vagrantfile(config, "box_win.rb", create_cachefd('box_win.rb'))
    print("Configuration files are ready")

    if not args.skip_packer_build:
        ret = run_packer(packer_tmpl, args)
    else:
        ret = 0

    if ret != 0:
        print("Packer failed. Build failed. Exiting...")
        sys.exit(3)

    if not args.skip_vagrant_box_add:
        ret = add_box(config, args)
    else:
        ret = 0

    if ret != 0:
        print("'vagrant box add' failed. Build failed. Exiting...")
        sys.exit(4)

    print(textwrap.dedent("""
    ===============================================================
    A base box was imported into your local Vagrant box repository.
    You should generate a Vagrantfile configuration in order to
    launch an instance of your box:

    malboxes spin {} <analysis_name>

    You can safely remove the {}/boxes/
    directory if you don't plan on hosting or sharing your base box.

    You can re-use this base box several times by using `malboxes
    spin`. Each VM will be independent of each other.
    ===============================================================""")
    .format(args.profile, DIRS.user_cache_dir))


def spin(parser, args):
    """
    Creates a Vagrantfile meant for user-interaction in the current directory.
    """
    if os.path.isfile('Vagrantfile'):
        print("Vagrantfile already exists. Please move it away. Exiting...")
        sys.exit(5)

    config, _ = prepare_config(args.profile)

    config['profile'] = args.profile
    config['name'] = args.name

    print("Creating a Vagrantfile")
    with open("Vagrantfile", 'w') as f:
        _prepare_vagrantfile(config, "analyst_single.rb", f)
    print("Vagrantfile generated. You can move it in your analysis directory "
          "and issue a `vagrant up` to get started with your VM.")


def append_to_script(filename, line):
    """ Appends a line to a file."""
    with open(filename, 'a') as f:
        f.write(line)


def add_to_user_scripts(profile):
    """ Adds the modified script to the user scripts file."""
    """ File names for the user scripts file and the script to be added."""
    filename = os.path.join(DIRS.user_config_dir, "scripts", "windows",
                            "user_scripts.ps1")
    line = "{}.ps1".format(profile)

    """ Check content of the user scripts file."""
    with open(filename, "r") as f:
        content = f.read()

    """ If script isnt present, add it."""
    if content.find(line) == -1:
        with open(filename, "a") as f:
            f.write(line)


def reg(parser, args):
    """
    Adds a registry key modification to a profile with PowerShell commands.
    """
    if args.modtype == "add":
        command = "New-ItemProperty"
        line = "{} -Path {} -Name {} -Value {} -PropertyType {}\r\n".format(
            command, args.key, args.name, args.value, args.valuetype)
        print("Adding: " + line)
    elif args.modtype == "modify":
        command = "Set-ItemProperty"
        line = "{0} -Path {1} -Name {2} -Value {3}\r\n".format(
                command, args.key, args.name, args.value)
        print("Adding: " + line)
    elif args.modtype == "delete":
        command = "Remove-ItemProperty"
        line = "{0} -Path {1} -Name {2}\r\n".format(
                command, args.key, args.name)
        print("Adding: " + line)
    else:
        print("Registry modification type invalid.")
        print("Valid ones are: add, delete and modify.")

    filename = os.path.join(DIRS.user_config_dir, "scripts", "user",
                            "windows", "{}.ps1".format(args.profile))
    append_to_script(filename, line)

    """ Adds the modified script to the user scripts."""
    add_to_user_scripts(args.profile)


def directory(parser, args):
    """ Adds the directory manipulation commands to the profile."""
    if args.modtype == "add":
        command = "New-Item"
        line = "{0} -Path {1} -Type directory\r\n".format(command,
                                                          args.dirpath)
        print("Adding directory: {}".format(args.dirpath))
    elif args.modtype == "delete":
        command = "Remove-Item"
        line = "{0} -Path {1}\r\n".format(
                command, args.dirpath)
        print("Removing directory: {}".format(args.dirpath))
    else:
        print("Directory modification type invalid.")
        print("Valid ones are: add, delete.")

    filename = os.path.join(DIRS.user_config_dir, "scripts", "user",
                            "windows", "{}.ps1".format(args.profile))
    append_to_script(filename, line)

    """ Adds the modified script to the user scripts."""
    add_to_user_scripts(args.profile)


def package(parser, args):
    """ Adds a package to install with Chocolatey."""
    line = "cinst {} -y\r\n".format(args.package)
    print("Adding Chocolatey package: {}".format(args.package))

    filename = os.path.join(DIRS.user_config_dir, "scripts", "user",
                            "windows", "{}.ps1".format(args.profile))
    append_to_script(filename, line)

    """ Adds the modified script to the user scripts."""
    add_to_user_scripts(args.profile)


def document(parser, args):
    """ Adds the file manipulation commands to the profile."""
    if args.modtype == "add":
        command = "New-Item"
        line = "{0} -Path {1}\r\n".format(command, args.docpath)
        print("Adding file: {}".format(args.docpath))
    elif args.modtype == "delete":
        command = "Remove-Item"
        line = "{0} -Path {1}\r\n".format(
                command, args.docpath)
        print("Removing file: {}".format(args.docpath))
    else:
        print("Directory modification type invalid.")
        print("Valid ones are: add, delete.")

    filename = os.path.join(DIRS.user_config_dir,
                    "scripts", "user", "windows",
                    "{}.ps1".format(args.profile))

    append_to_script(filename, line)

    """ Adds the modified script to the user scripts."""
    add_to_user_scripts(args.profile)


def main():
    global DEBUG
    try:
        parser, args = initialize()
        if args.debug:
            DEBUG = True
        args.func(parser, args)

    finally:
        cleanup()


if __name__ == "__main__":
    main()
