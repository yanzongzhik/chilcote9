#!/usr/bin/python2.7
"""
Base image creation script.

This script processes catalog(s) of packages, verifies the sha1 
hash of each package, downloads any missing or updated packages, 
and compiles a new image consisiting of a given base OS X installer 
and the list of packages.

(Using a webserver to store packages and installers is encouraged,
but not required.)

Usage: sudo stew.py [options]

Options:
  -h, --help            show this help message and exit
  -b CATALOG, --build=CATALOG
                        Specify catalog to process
  -c, --configure       Set up or recreate configuration file
  -u PACKAGE, --upload=PACKAGE
                        Upload package to webserver
  -C FILENAME, --checksum=FILENAME
                        Return checksum of a cached package
"""

##############################################################################
# Copyright 2014 Joseph Chilcote
# 
#  Licensed under the Apache License, Version 2.0 (the "License"); you may not
#  use this file except in compliance with the License. You may obtain a copy
#  of the License at  
# 
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.
##############################################################################

import os
import re
import sys
import shutil
import hashlib
import urllib2
import logging
import datetime
import platform
import subprocess
from optparse import OptionParser

current_os = platform.mac_ver()[0]
base_pkgs = []
other_pkgs = []
CONFIG = os.path.join(os.getenv('HOME'), '.stew_config')
BUILD = os.path.join(os.getcwd(), 'build')
CACHE = os.path.join(os.getcwd(), 'cache')
LOG = os.path.join(os.getcwd(), 'log')
OUTPUT = os.path.join(os.getcwd(), 'output')
project_dirs = [BUILD, CACHE, LOG, OUTPUT]
timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M')
log_file = os.path.join(LOG, '%s.log' % timestamp)

class Stew(object):
    """Object for building the image."""

    def __init__(self, volume_name, output_name, config, 
                    base_installer, pkgs, BUILD):
        self.cwd = os.getcwd()
        self.build = BUILD
        self.volume_name = volume_name
        self.output_name = output_name
        self.config = config
        self.base_installer = base_installer
        self.pkgs = pkgs
        #self.timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M')
        self.os_version = run_cmd(['sw_vers'])[0].split('\t')[2][:4]
        self.installer_choices = '%s_InstallerChoices.xml' % self.os_version
        self.osbuild = self.base_installer.split("_")[1]
        self.sb_cache = os.path.join(CACHE, self.osbuild) + '.sparsebundle'
        self.sb_cache_exists = False

    def setup_build_folder(self):
        """Creates the build directory if missing."""
        try:
            os.mkdir(self.build)
        except OSError:
            try:
                os.mkdir(os.path.dirname(self.build))
                os.mkdir(self.build)
            except OSError:
                logging.error('Unable to create build folder %s' % self.build)
       
    def create_sparsebundle(self, sb_cache=None):
        """Creates the sparsebundle on which to install the base system."""
        print "Preparing build environment..."
        sparsebundle_path = os.path.join(self.build, 'stew.sparsebundle')
        if os.path.exists(sparsebundle_path):
            shutil.rmtree(sparsebundle_path)
        if not sb_cache:
            cmd = ['hdiutil', 'create', sparsebundle_path, '-size', '30G',
                        '-volname', self.volume_name, '-layout', 'GPTSPUD',
                        '-fs', 'JHFS+', '-mode','775', '-uid', '0',
                        '-gid', '80']
            (unused_stdout, stderr, unused_rc) = run_cmd(cmd)
            if stderr:
                logging.warning('Failed to create %s: %s' % (sparsebundle_path,
                                                                stderr))
                raise SystemExit
            else:
                return sparsebundle_path
        else:
            logging.debug('Found sparsebundle cache: %s' % sb_cache)
            logging.debug('Copying %s to %s' % (sb_cache, sparsebundle_path))
            shutil.copytree(os.path.join(CACHE, sb_cache), 
                                sparsebundle_path)        
        return sparsebundle_path

    def mount_sparsebundle(self, sparsebundle):
        """Mounts the sparsebundle to prepare for install."""
        cmd = ['hdiutil', 'attach', '-owners', 'on', '-nobrowse',
                '-noverify', '-noautoopen', sparsebundle]
        logging.debug('Mounting %s' % sparsebundle)
        (stdout, stderr, unused_rc) = run_cmd(cmd)
        if stderr:
            logging.error('Unable to mount %s: %s' % (sparsebundle, stderr))
        else:
            if self.sb_cache_exists and current_os != '10.10':
                return stdout.split('\n')[-3].split('\t')[-1]
            else:
                return stdout.split('\n')[-2].split('\t')[-1]

    def mount_installer(self):
        """Mounts the InstallDMG volume"""
        baseimage_path = os.path.join(CACHE, self.base_installer)
        cmd = ['hdiutil', 'attach', '-nobrowse','-noverify', 
                                '-noautoopen', baseimage_path]
        logging.debug('Mounting %s' % baseimage_path)
        (stdout, stderr, unused_rc) = run_cmd(cmd)
        if stderr:
            logging.error('Unable to mount %s: %s' % (baseimage_path, stderr))
            raise SystemExit
        else:
            return stdout.split('\n')[-2].split('\t')[-1]

    def install_base(self, mountpoint, installer_mount):
        """Installs the base system into sparsebundle"""
        cmd = ['installer', '-pkg', '%s/Packages/OSInstall.mpkg' % installer_mount,
                                            '-target', mountpoint]
        logging.info('Installing OS X into %s...' % mountpoint)
        (unused_stdout, stderr, unused_rc) = run_cmd(cmd)
        cmd = ['hdiutil', 'detach', installer_mount]
        logging.debug('Detaching %s' % installer_mount)
        (unused_stdout, stderr, unused_rc) = run_cmd(cmd)
        if stderr:
            logging.error('Failed to unmount: %s' % stderr)

    def detach_mountpoint(self, mountpoint, sparsebundle):
        """Detaches sparsebundle"""
        logging.debug('Detaching %s' % mountpoint)
        cmd = ['hdiutil', 'detach', '-force', mountpoint]
        (unused_stdout, stderr, unused_rc) = run_cmd(cmd)
        if stderr:
            logging.error('Unable to detach %s: %s' % (mountpoint, sparsebundle))

    def cache_base(self, sparsebundle):
        logging.debug('Saving cache to %s' % self.sb_cache)
        shutil.copytree(sparsebundle, self.sb_cache)

    def mount_dmg(self, dmg):
        dmg_path = os.path.join(CACHE, dmg)
        cmd = ['hdiutil', 'attach', '-nobrowse','-noverify', 
                                '-noautoopen', dmg_path]
        logging.debug('Mounting %s' % dmg_path)
        (stdout, stderr, unused_rc) = run_cmd(cmd)
        if stderr:
            logging.error('Unable to mount %s: %s' % (dmg_path, stderr))
        else:
            return stdout.split('\n')[-2].split('\t')[-1]

    def install_packages(self, pkgs, mountpoint):
        """Installs all pkgs into sparsebundle"""
        print 'Installing packages...'
        for pkg in pkgs:
            if pkg[0].endswith('.dmg'):
                dmg_mount = self.mount_dmg(pkg[0])
                for f in os.listdir(dmg_mount):
                    if f.endswith('.pkg') or f.endswith('.mpkg'):
                        pkg_to_install = os.path.join(dmg_mount, f)
            elif pkg[0].endswith('.pkg'):
                dmg_mount = False
                pkg_to_install = os.path.join(CACHE, pkg[0])
            logging.debug('Installing %s' % pkg_to_install)
            cmd = ['installer', '-pkg', pkg_to_install, '-target', 
                                    mountpoint, '-verboseR']
            (stdout, stderr, unused_rc) = run_cmd(cmd)
            if stderr:
                logging.error('Failure installing %s: %s' % (pkg_to_install, stderr))
            else:
                logging.info('Successfully installed %s' % pkg_to_install)
            if dmg_mount:
                self.detach_mountpoint(dmg_mount, pkg[0])

    def convert_sparsebundle(self, mountpoint, sparsebundle):
        """Detaches and converts sparsebundle"""
        print 'Converting and scanning disk image for restore...'
        if os.path.basename(mountpoint) != self.volume_name:
            logging.debug('Renaming %s to %s' % (mountpoint, self.volume_name))
            cmd = ['diskutil', 'renameVolume', mountpoint, self.volume_name]
            (unused_stdout, stderr, unused_rc) = run_cmd(cmd)
            if stderr:
                logging.error('Unable to rename %s to %s' % (mountpoint, self.volume_name))
            else:
                mountpoint = "/Volumes/%s" % self.volume_name
        logging.debug('Detaching %s' % mountpoint)
        cmd = ['hdiutil', 'detach', '-force', mountpoint]
        (unused_stdout, stderr, unused_rc) = run_cmd(cmd)
        if stderr:
            logging.error('Unable to detach %s: %s' % (mountpoint, sparsebundle))
        else:
            if self.output_name.endswith(".dmg"):
                self.output_name = self.output_name[:-4]
            image_name = '%s_%s.hfs.dmg' % (self.output_name, timestamp)
            image_file = os.path.join(OUTPUT, image_name)
            logging.debug('Converting %s to %s' % (sparsebundle, image_file))
            cmd = ['hdiutil', 'convert', sparsebundle, '-format', 
                                'UDZO', '-o', image_file]
            (unused_stdout, stderr, unused_rc) = run_cmd(cmd)
            if stderr:
                logging.error('Image conversion failed: %s' % stderr)
            else:
                logging.debug('ASR imagescanning %s' % image_file)
                cmd = ['asr', 'imagescan', '-source', image_file]
                run_cmd(cmd)
                return image_file

    def cleanup(self, sparsebundle):
        """Removes temporary files"""
        try:
            shutil.rmtree(sparsebundle)
        except OSError, e:
            logging.error('Could not remove sparsebundle %s: %s' % (sparsebundle, e))

    def build_image(self):
        """Builds the image"""
        if os.path.exists(self.sb_cache):
            self.sb_cache_exists = True
        if self.sb_cache_exists:
            sparsebundle = self.create_sparsebundle(sb_cache=self.sb_cache)
        else:
            sparsebundle = self.create_sparsebundle()
        mountpoint = self.mount_sparsebundle(sparsebundle)
        if not self.sb_cache_exists:
            installer_mount = self.mount_installer()
            self.install_base(mountpoint, installer_mount)
            self.detach_mountpoint(mountpoint, sparsebundle)
            self.cache_base(sparsebundle)
            self.sb_cache_exists = True
            mountpoint = self.mount_sparsebundle(sparsebundle)
        self.install_packages(self.pkgs, mountpoint)
        image_output = self.convert_sparsebundle(mountpoint, sparsebundle)
        print 'Image saved to: %s' % image_output
        self.cleanup(sparsebundle)

def run_cmd(cmd, stream_out=False):
    """Runs a command and returns a tuple of stdout, stderr, returncode."""
    if stream_out:
        task = subprocess.Popen(cmd)
    else:
        task = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    (stdout, stderr) = task.communicate()
    return stdout, stderr, task.returncode

def create_dir(dirpath):
    """Creates the given directory if missing."""
    dirpath = os.path.join(os.getcwd(), dirpath)
    try:
        os.mkdir(dirpath)
    except OSError:
        try:
            os.mkdir(os.path.dirname(dirpath))
            os.mkdir(dirpath)
        except OSError:
            print 'Unable to create folder %s' % dirpath

def collect_config_info(prompt):
    """Displays prompt to user and collects the response."""
    return raw_input('%s' % prompt)

def append_config(config, category, prompt):
    """Appends the config file with user-provided info."""
    unanswered = True
    while unanswered:
        try:
            answer = collect_config_info(prompt)
        except KeyboardInterrupt:
            logging.error('Aborting configuration.')
            sys.exit()
        if answer != '':
            unanswered = False
    f = open(config, 'a')
    f.write("%s=%s\n" % (category, answer))
    f.close

def get_config_data(config, category):
    """Returns data from config file."""
    f = open(config, 'r')
    for line in f.readlines():
        if category in line:
            line = line.rstrip()
            return line.split('=')[1]
    f.close

def get_catalog_data(catalog, category):
    """Returns requested info as defined in the catalog."""
    f = open(catalog, 'r')
    for line in f.readlines():
        if category in line:
            line = line.rstrip()
            if category == "base-catalog":
                return "catalogs/%s" % line.split()[1]
            else:
                return line.split()[1]
    f.close

def get_pkgs(catalog, pkgs):
    """Populates package list based on the catalog."""
    f = open(catalog, 'r')
    for line in f.readlines():
            if not "base-catalog" in line and not "base-installer" in line \
                                    and not "volume-name" in line \
                                    and not "output-name" in line:
                line = line.rstrip()
                if line != '':
                    pkgs.append(line.split())
    f.close

def process_base_installer(base, webserver, webpath):
    """Checks for base installer and dowloads if needed."""
    pkg_url = "http://%s/%s/%s" % (webserver, webpath, base)
    if os.path.exists(os.path.join(os.getcwd(), 'cache', base)):
        logging.debug('%s found in cache' % base)
    else:
        logging.debug('%s needs to be downloaded' % base)
        logging.debug('Downloading %s' % pkg_url)
        download_target = '%s/%s' % (CACHE, base)
        download_pkg(pkg_url, download_target)
        logging.info('%s downloaded to cache' % base)

def process_pkgs(pkgs, webserver, webpath):
    """Checks for package(s) and dowloads if needed."""
    for pkg in pkgs:
        downloaded = False
        verified = False 
        pkg_url = 'http://%s/%s/%s' % (webserver, webpath, pkg[0])
        if not os.path.exists(os.path.join(os.getcwd(), 'cache', pkg[0])):
            logging.debug('%s missing and needs to be downloaded' % pkg[0])
            logging.debug('Downloading %s' % pkg_url)
            download_target = "%s/%s" % (CACHE, pkg[0])
            download_pkg(pkg_url, download_target)
            downloaded = True
        l_sha1 = get_checksum(os.path.join(os.getcwd(), 'cache', pkg[0]))
        if l_sha1 != pkg[1]:
            logging.debug('%s sha1 mismatch and needs to be downloaded' % pkg[0])
            logging.debug('downloading %s' % pkg_url)
            os.remove(os.path.join(CACHE, pkg[0]))
            download_target = "%s/%s" % (CACHE, pkg[0])
            download_pkg(pkg_url, download_target)
            downloaded = True            
            l_sha1 = get_checksum(os.path.join(os.getcwd(), 'cache', pkg[0]))
            if l_sha1 != pkg[1]:
                logging.warning('WARNING: %s does not match catalog.' % pkg[0])
                sys.exit()
        if downloaded:
            logging.info('%s downloaded and verified' % pkg[0])
        else:
            logging.info('%s found in cache and verified' % pkg[0])

def download_pkg(pkgurl, target):
    """Downloads package."""
    try:
        pkg_dl = urllib2.urlopen(pkgurl)
        tmpfile = open(target, 'wb')
        shutil.copyfileobj(pkg_dl, tmpfile)
    except urllib2.URLError, e:
        logging.warning('Download of %s failed with error %s' % (pkgurl, e))
        sys.exit()
    except IOError, e:
        logging.error('Could not write %s to disk; check disk permissions!' % target)
        logging.error('Error: %s' % e)
        sys.exit()

def upload_pkg(pkg, login, webserver, serverpath):
    """Uploads package."""
    cmd = ['scp', pkg, '%s@%s:%s' % (login, webserver, serverpath)]
    run_cmd(cmd)

def get_checksum(pkg):
    """Returns sha1 checksum of package."""
    statinfo = os.stat(pkg)
    if statinfo.st_size/1048576 < 200:
        f_content = open(pkg, 'r').read()
        f_hash = hashlib.sha1(f_content).hexdigest()
        return f_hash
    else:
        cmd = ['shasum', pkg]
        (stdout, unused_sterr, unused_rc) = run_cmd(cmd)
        return stdout.split()[0]

def main():
    parser = OptionParser(usage='Usage: sudo ./%prog [options]')
    parser.add_option('-b', '--build', dest='catalog',
                        help='Specify catalog to process')
    parser.add_option('-c', '--configure', dest='configure', 
                        action='store_true',
                        help='Set up or recreate configuration file')
    parser.add_option('-u', '--upload', dest='package',
                        help='Upload package to webserver')
    parser.add_option('-C', '--checksum', dest='filename',
                        help='Return checksum of a cached package')

    (options, unused_args) = parser.parse_args()

    if options.configure or not os.path.exists(CONFIG):
        for d in project_dirs:
            if not os.path.exists(d):
                create_dir(d)
        if not os.path.exists(CONFIG):
            print 'Config file does not exist! Creating now.'
        else:
            print 'Recreating config file.'
            os.remove(CONFIG)
        append_config(CONFIG, 'webserver', 'Enter the FQDN of your webserver '
                            '(i.e. mypackageserver.example.com)\n'
                            'Server FQDN: ')
        append_config(CONFIG, 'path', 'Enter the storage path on your '
                            'webserver (i.e. /var/www/html/packages)\n'
                            'Server path: ')
        append_config(CONFIG, 'login', 'Enter the ssh user for your '
                            ' webserver\nLogin user: ')
        print '\nCongiration file setup complete!' \
                            '\nUse SSH keys for best results!\n'
        if not options.configure:
            parser.print_help()
        sys.exit(0)
    webserver = get_config_data(CONFIG, 'webserver')
    serverpath = get_config_data(CONFIG, 'path')
    webpath = os.path.basename(serverpath)
    login = get_config_data(CONFIG, 'login')
    if options.package:
        package = options.package
        print 'Uploading %s to %s:%s' % (package, webserver, serverpath)
        upload_pkg(package, login, webserver, serverpath)
        print os.path.basename(package), get_checksum(package)
        sys.exit(0)
    if options.filename:
        package = options.filename
        if "cache" not in options.filename:
            package = "cache/%s" % package
        print os.path.basename(package), get_checksum(package)
        sys.exit(0)
    if options.catalog:
        if os.getuid() != 0:
            print 'Using the -b option requires root!'
            parser.print_help()
            sys.exit(1)
        logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S %p',
                            level=logging.DEBUG,
                            filename=log_file)

        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter('%(message)s')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)            
        catalog = options.catalog
        if not "catalogs" in catalog:
            catalog = "catalogs/%s" % options.catalog
        if not os.path.exists(catalog):
            print "File %s does not exist." % catalog
            sys.exit(1)
        volume_name = get_catalog_data(catalog, "volume-name")
        output_name = get_catalog_data(catalog, "output-name")
        base_catalog = get_catalog_data(catalog, "base-catalog")
        if base_catalog:
            get_pkgs(base_catalog, base_pkgs)
            base_installer = get_catalog_data(base_catalog, "base-installer")
            if not volume_name:
                volume_name = get_catalog_data(base_catalog, "volume-name")
            if not output_name:
                output_name = get_catalog_data(base_catalog, "output-name")
        else:
            base_installer = get_catalog_data(catalog, "base-installer")
        if not volume_name:
            volume_name = "Macintosh HD"
        if not output_name:
            output_name = '%s.dmg' % base_installer.split("_")[0]
        logging.info('Build started at: %s' % datetime.datetime.now())
        logging.info('Current OS: %s' % current_os)
        logging.info('Base catalog: %s' % base_catalog)
        logging.info('Base installer: %s' % base_installer)
        logging.info('Volume name: %s' % volume_name)
        logging.info('Output name: %s' %output_name)
        get_pkgs(catalog, other_pkgs)
        all_pkgs = base_pkgs + other_pkgs
        print 'Processing catalog(s)...'
        process_base_installer(base_installer, webserver, webpath)
        process_pkgs(all_pkgs, webserver, webpath)
        stew = Stew(volume_name, output_name, CONFIG, base_installer,
                        all_pkgs, BUILD)
        stew.build_image()
        logging.info('Build finished at %s' % datetime.datetime.now())
    else:
        parser.print_help()

if __name__ == '__main__':
  main()