#! /usr/bin/env python
# -*- coding: utf-8 -*-
"""
GPlay-Cli
Copyleft (C) 2015 Matlink
Hardly based on GooglePlayDownloader https://framagit.org/tuxicoman/googleplaydownloader
Copyright (C) 2013 Tuxicoman

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any
later version.
This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
details.
You should have received a copy of the GNU Affero General Public License along with this program.  If not,
see <http://www.gnu.org/licenses/>.
"""

from __future__ import print_function

import sys
import os
import logging
import argparse
import requests
import shutil
import stat

from enum import IntEnum
from gpapi.googleplay import GooglePlayAPI
from gpapi.googleplay import LoginError
from pkg_resources import get_distribution, DistributionNotFound

try:  # Python3+
    from pyaxmlparser import APK  # Pyaxmlparser
    import configparser
    unicode = None
    read_input = input
except ImportError:  # Python2
    import ConfigParser as configparser
    from androguard.core.bytecodes.apk import APK as androguard_apk  # Androguard

    class APK(androguard_apk):
        @property
        def get_androidversion_code(self):
            return androguard_apk.get_androidversion_code(self)
        version_code = get_androidversion_code
    read_input = raw_input

try:
    import keyring
    HAVE_KEYRING = True
except ImportError:
    HAVE_KEYRING = False


try:
    __version__ = '%s [Python%s] ' % (get_distribution('gplaycli').version, sys.version.split()[0])
except DistributionNotFound:
    __version__ = 'unknown: gplaycli not installed (version in setup.py)'


class ERRORS(IntEnum):
    OK = 0
    TOKEN_DISPENSER_AUTH_ERROR = 5
    TOKEN_DISPENSER_SERVER_ERROR = 6
    KEYRING_NOT_INSTALLED = 10
    CANNOT_LOGIN_GPLAY = 15


class GPlaycli(object):
    def __init__(self, args=None, credentials=None):
        # no config file given, look for one
        if credentials is None:
            # default local user configs
            cred_paths_list = [
                'gplaycli.conf',
                os.path.expanduser("~") + '/.config/gplaycli/gplaycli.conf',
                '/etc/gplaycli/gplaycli.conf'
            ]
            tmp_list = list(cred_paths_list)
            while not os.path.isfile(tmp_list[0]):
                tmp_list.pop(0)
                if not tmp_list:
                    raise OSError("No configuration file found at %s" % cred_paths_list)
            credentials = tmp_list[0]

        default_values = {
        }
        self.configparser = configparser.ConfigParser(default_values)
        self.configparser.read(credentials)
        self.config = dict()
        for key, value in self.configparser.items("Credentials"):
            self.config[key] = value

        self.tokencachefile = os.path.expanduser(self.configparser.get("Cache", "token"))

        # default settings, ie for API calls
        if args is None:
            self.yes = False
            self.verbose = False
            logging.basicConfig()
            self.progress_bar = False
            self.logging = False
            self.device_codename = 'bacon'

        # if args are passed
        else:
            self.yes = args.yes_to_all
            self.verbose = args.verbose
            if self.verbose:
                logging.basicConfig(level=logging.INFO, format=("[%(levelname)s] %(message)s"))
            logging.info('GPlayCli version %s' % __version__)
            logging.info('Configuration file is %s' % credentials)
            self.progress_bar = args.progress_bar
            self.set_download_folder(args.update_folder)
            self.logging = args.enable_logging
            self.device_codename = args.device_codename
            if args.token_enable is None:
                self.token_enable = self.configparser.getboolean('Credentials', 'token')
            if self.token_enable:
                if args.token_url is None:
                    self.token_url = self.configparser.get('Credentials', 'token_url')
                else:
                    self.token_url = args.token_url
                self.token, self.gsfid = self.retrieve_token()

            if self.logging:
                self.success_logfile = "apps_downloaded.log"
                self.failed_logfile = "apps_failed.log"
                self.unavail_logfile = "apps_not_available.log"

    def get_cached_token(self):
        try:
            with open(self.tokencachefile, 'r') as tcf:
                token, gsfid = tcf.readline().split()
                if not token:
                    token = None
                    gsfid = None
        except (IOError, ValueError):  # cache file does not exists or is corrupted
            token = None
            gsfid = None
        return token, gsfid

    def write_cached_token(self, token, gsfid):
        try:
            # creates cachefir if not exists
            cachedir = os.path.dirname(self.tokencachefile)
            if not os.path.exists(cachedir):
                os.mkdir(cachedir)
            with open(self.tokencachefile, 'w') as tcf:
                tcf.write("%s %s" % (token, gsfid))
        except IOError as e:
            raise IOError("Failed to write token to cache file: %s %s" % (self.tokencachefile, e.strerror))

    def retrieve_token(self, force_new=False):
        token, gsfid = self.get_cached_token()
        if token is not None and not force_new:
            logging.info("Using cached token.")
            return token, gsfid
        logging.info("Retrieving token ...")
        r = requests.get(self.token_url)
        if r.text == 'Auth error':
            print('Token dispenser auth error, probably too many connections')
            sys.exit(ERRORS.TOKEN_DISPENSER_AUTH_ERROR)
        elif r.text == "Server error":
            print('Token dispenser server error')
            sys.exit(ERRORS.TOKEN_DISPENSER_SERVER_ERROR)
        token, gsfid = r.text.split(" ")
        logging.info("Token: %s" % token)
        logging.info("GSFId: %s" % gsfid)
        self.token = token
        self.gsfid = gsfid
        self.write_cached_token(token, gsfid)
        return token, gsfid

    def set_download_folder(self, folder):
        self.config["download_folder_path"] = folder

    def connect_to_googleplay_api(self):
        api = GooglePlayAPI(device_codename=self.device_codename)
        error = None
        try:
            if self.token is False:
                logging.info("Using credentials to connect to API")
                username = self.config["gmail_address"]
                passwd = None
                if self.config["gmail_password"]:
                    logging.info("Using plaintext password")
                    passwd = self.config["gmail_password"]
                elif self.config["keyring_service"] and HAVE_KEYRING is True:
                    passwd = keyring.get_password(self.config["keyring_service"], username)
                elif self.config["keyring_service"] and HAVE_KEYRING is False:
                    print("You asked for keyring service but keyring package is not installed")
                    sys.exit(ERRORS.KEYRING_NOT_INSTALLED)
                api.login(email=username, password=passwd)
            else:
                logging.info("Using token to connect to API")
                api.login(authSubToken=self.token, gsfId=int(self.gsfid, 16))
        except LoginError as exc:
            error = exc.value
            success = False
        else:
            self.playstore_api = api
            try:
                self.raw_search(list(), 'firefox', 1)
            except (ValueError, IndexError) as ve:  # invalid token or expired
                logging.info("Token has expired or is invalid. Retrieving a new one...")
                self.retrieve_token(force_new=True)
                api.login(authSubToken=self.token, gsfId=int(self.gsfid, 16))
            success = True
        return success, error

    # List apks in the given folder
    def list_folder_apks(self, folder):
        list_of_apks = [filename for filename in os.listdir(folder) if filename.endswith(".apk")]
        return list_of_apks

    def prepare_analyse_apks(self):
        download_folder_path = self.config["download_folder_path"]
        list_of_apks = [filename for filename in os.listdir(download_folder_path) if
                        os.path.splitext(filename)[1] == ".apk"]
        if list_of_apks:
            logging.info("Checking apks ...")
            self.analyse_local_apks(list_of_apks, self.playstore_api, download_folder_path,
                                    self.prepare_download_updates)

    def analyse_local_apks(self, list_of_apks, playstore_api, download_folder_path, return_function):
        list_apks_to_update = []
        package_bunch = []
        for position, filename in enumerate(list_of_apks):
            filepath = os.path.join(download_folder_path, filename)
            a = APK(filepath)
            packagename = a.package
            package_bunch.append(packagename)

        # BulkDetails requires only one HTTP request
        # Get APK info from store
        details = playstore_api.bulkDetails(package_bunch)
        for detail, packagename, filename in zip(details, package_bunch, list_of_apks):
            logging.info("Analyzing %s" % packagename)
            # Getting Apk infos
            filepath = os.path.join(download_folder_path, filename)
            a = APK(filepath)
            apk_version_code = a.version_code
            store_version_code = detail['versionCode']

            # Compare
            if apk_version_code != "" and int(apk_version_code) < int(store_version_code) and int(
                    store_version_code) != 0:
                # Add to the download list
                list_apks_to_update.append([packagename, filename, int(apk_version_code), int(store_version_code)])

        return_function(list_apks_to_update)

    def prepare_download_updates(self, list_apks_to_update):
        if list_apks_to_update:
            list_of_packages_to_download = []

            # Ask confirmation before downloading
            message = "The following applications will be updated :"
            for packagename, filename, apk_version_code, store_version_code in list_apks_to_update:
                message += "\n%s Version : %s -> %s" % (filename, apk_version_code, store_version_code)
                list_of_packages_to_download.append([packagename, filename])
            message += "\n"
            print(message)
            if not self.yes:
                print("\nDo you agree?")
                return_value = read_input('y/n ?')

            if self.yes or return_value == 'y':
                logging.info("Downloading ...")
                downloaded_packages = self.download_selection(self.playstore_api, list_of_packages_to_download,
                                                              self.after_download)
                return_string = str()
                for package in downloaded_packages:
                    return_string += package + " "
                print("Updated: " + return_string[:-1])
        else:
            print("Everything is up to date !")
            sys.exit(ERRORS.OK)

    def download_selection(self, playstore_api, list_of_packages_to_download, return_function):
        success_downloads = list()
        failed_downloads = list()
        unavail_downloads = list()

        # BulkDetails requires only one HTTP request
        # Get APK info from store
        details = playstore_api.bulkDetails([pkg[0] for pkg in list_of_packages_to_download])
        position = 1
        for detail, item in zip(details, list_of_packages_to_download):
            packagename, filename = item

            logging.info("%s / %s %s" % (position, len(list_of_packages_to_download), packagename))

            # Check for download folder
            download_folder_path = self.config["download_folder_path"]
            if not os.path.isdir(download_folder_path):
                os.mkdir(download_folder_path)

            # Get the version code and the offer type from the app details
            # m = playstore_api.details(packagename)
            vc = detail['versionCode']

            # Download
            try:
                if detail['offer'][0]['checkoutFlowRequired']:
                    data = playstore_api.delivery(packagename, vc, progress_bar=self.progress_bar)
                else:
                    data = playstore_api.download(packagename, vc, progress_bar=self.progress_bar)
                success_downloads.append(packagename)
            except IndexError as exc:
                print("Error while downloading %s : %s" % (packagename,
                                                           "this package does not exist, "
                                                           "try to search it via --search before"))
                unavail_downloads.append((item, exc))
            except Exception as exc:
                print("Error while downloading %s : %s" % (packagename, exc))
                failed_downloads.append((item, exc))
            else:
                if filename is None:
                    filename = packagename + ".apk"
                filepath = os.path.join(download_folder_path, filename)

                try:
                    open(filepath, "wb").write(data)
                except IOError as exc:
                    print("Error while writing %s : %s" % (packagename, exc))
                    failed_downloads.append((item, exc))
            position += 1

        success_items = set(success_downloads)
        failed_items = set([item[0] for item, error in failed_downloads])
        unavail_items = set([item[0] for item, error in unavail_downloads])
        to_download_items = set([item[0] for item in list_of_packages_to_download])

        if self.logging:
            self.write_logfiles(success_items, failed_items, unavail_items)

        return_function(failed_downloads + unavail_downloads)
        return to_download_items - failed_items

    def after_download(self, failed_downloads):
        # Info message
        if not failed_downloads:
            message = "Download complete"
        else:
            message = "A few packages could not be downloaded :"
            for item, exception in failed_downloads:
                package_name, filename = item
                if filename is not None:
                    message += "\n%s : %s" % (filename, package_name)
                else:
                    message += "\n%s" % package_name
                message += "\n%s\n" % exception

        print(message)

    def sizeof_fmt(self, num):
        for x in ['bytes', 'KB', 'MB', 'GB', 'TB']:
            if num < 1024.0:
                return "%3.1f%s" % (num, x)
            num /= 1024.0

    def raw_search(self, results_list, search_string, nb_results):
        # Query results
        return self.playstore_api.search(search_string, nb_result=nb_results)

    def search(self, results_list, search_string, nb_results, free_only=True, include_headers=True):
        try:
            results = self.raw_search(results_list, search_string, nb_results)
        except IndexError:
            results = list()
        if not results:
            print("No result")
            return
        all_results = list()
        if include_headers:
            # Name of the columns
            col_names = ["Title", "Creator", "Size", "Downloads", "Last Update", "AppID", "Version", "Rating"]
            all_results.append(col_names)
        # Compute results values
        for result in results:
            if free_only and result['offer'][0]['checkoutFlowRequired']:  # if not Free to download
                continue
            l = [result['title'],
                 result['author'],
                 self.sizeof_fmt(result['installationSize']),
                 result['numDownloads'],
                 result['uploadDate'],
                 result['docId'],
                 result['versionCode'],
                 "%.2f" % result["aggregateRating"]["starRating"]
                ]
            for indice, item in enumerate(l):
                if type(item) is unicode:
                    l[indice] = item.encode('utf-8')
            if len(all_results) < int(nb_results) + 1:
                all_results.append(l)

        if self.verbose:
            # Print a nice table
            col_width = list()
            for column_indice in range(len(all_results[0])):
                col_length = max([len("%s" % row[column_indice]) for row in all_results])
                col_width.append(col_length + 2)

            for result in all_results:
                print("".join(str("%s" % item).strip().ljust(col_width[indice]) for indice, item in
                              enumerate(result)))
        return all_results

    def download_packages(self, list_of_packages_to_download):
        self.download_selection(self.playstore_api, [(pkg, None) for pkg in list_of_packages_to_download],
                                self.after_download)

    def write_logfiles(self, success, failed, unavail):
        if success:
            with open(self.success_logfile, 'w') as logfile:
                for package in success:
                    logfile.write('%s\n' % package)

        if failed:
            with open(self.failed_logfile, 'w') as logfile:
                for package in failed:
                    logfile.write('%s\n' % package)

        if unavail:
            with open(self.unavail_logfile, 'w') as logfile:
                for package in unavail:
                    logfile.write('%s\n' % package)


def install_cronjob(automatic=False):
    cred_default = '/etc/gplaycli/gplaycli.conf'
    fold_default = '/opt/apks'
    frequence_default = "/etc/cron.daily"

    if not automatic:
        credentials = read_input('path to gplaycli.conf? let empty for ' + cred_default + '\n') or cred_default
        folder_to_update = read_input('path to apks folder? let empty for ' + fold_default + '\n') or fold_default
        frequence = read_input('update it [d]aily or [w]eekly?\n')
        if frequence == 'd':
            frequence_folder = '/etc/cron.daily'
        elif frequence == 'w':
            frequence_folder = '/etc/cron.weekly'
        else:
            raise Exception('please type d/w to make your choice')

    else:
        credentials = cred_default
        folder_to_update = fold_default
        frequence_folder = frequence_default

    frequence_file = frequence_folder + '/gplaycli'
    shutil.copyfile('/etc/gplaycli/cronjob', frequence_file)

    with open('/etc/gplaycli/cronjob', 'r') as fi:
        with open(frequence_file, 'w') as fo:
            for line in fi:
                line = line.replace('PL_FOLD', folder_to_update)
                line = line.replace('PL_CRED', credentials)
                fo.write(line)

    st = os.stat(frequence_file)
    os.chmod(frequence_file, st.st_mode | stat.S_IEXEC)
    print('Cronjob installed at ' + frequence_file)
    return ERRORS.OK


def load_from_file(filename):
    return [package.strip('\r\n') for package in open(filename).readlines()]


def main():
    parser = argparse.ArgumentParser(description="A Google Play Store Apk downloader and manager for command line")
    parser.add_argument('-V', '--version', action='store_true', dest='version', help='Print version number and exit')
    parser.add_argument('-y', '--yes', action='store_true', dest='yes_to_all', help='Say yes to all prompted questions')
    parser.add_argument('-l', '--list', action='store', dest='list', metavar="FOLDER",
                        type=str, help="List APKS in the given folder, with details")
    parser.add_argument('-s', '--search', action='store', dest='search_string', metavar="SEARCH",
                        type=str, help="Search the given string in Google Play Store")
    parser.add_argument('-P', '--paid', action='store_true', dest='paid',
                        default=False, help='Also search for paid apps')
    parser.add_argument('-n', '--number', action='store', dest='number_results', metavar="NUMBER",
                        type=int, help="For the search option, returns the given number of matching applications")
    parser.add_argument('-d', '--download', action='store', dest='packages_to_download', metavar="AppID", nargs="+",
                        type=str, help="Download the Apps that map given AppIDs")
    parser.add_argument('-F', '--file', action='store', dest='load_from_file', metavar="FILE",
                        type=str, help="Load packages to download from file, one package per line")
    parser.add_argument('-u', '--update', action='store', dest='update_folder', metavar="FOLDER",
                        type=str, help="Update all APKs in a given folder")
    parser.add_argument('-f', '--folder', action='store', dest='dest_folder', metavar="FOLDER", nargs=1,
                        type=str, default=".", help="Where to put the downloaded Apks, only for -d command")
    parser.add_argument('-dc', '--device-codename', action='store', dest='device_codename', metavar="DEVICE_CODENAME",
                        type=str, default="bacon", help="The device codename to fake", choices=GooglePlayAPI.getDevicesCodenames())
    parser.add_argument('-t', '--token', action='store_true', dest='token_enable', default=None,
                        help='Instead of classical credentials, use the tokenize version')
    parser.add_argument('-tu', '--token-url', action='store', dest='token_url', metavar="TOKEN_URL",
                        type=str, default=None, help="Use the given tokendispenser URL to retrieve a token")
    parser.add_argument('-v', '--verbose', action='store_true', dest='verbose', help='Be verbose')
    parser.add_argument('-c', '--config', action='store', dest='config', metavar="CONF_FILE", nargs=1,
                        type=str, default=None, help="Use a different config file than gplaycli.conf")
    parser.add_argument('-p', '--progress', action='store_true', dest='progress_bar',
                        help='Prompt a progress bar while downloading packages')
    parser.add_argument('-L', '--log', action='store_true', dest='enable_logging', default=False,
                        help='Enable logging of apps status. Downloaded, failed, not available apps will be written in separate logging files')
    parser.add_argument('-ic', '--install-cronjob', action='store_true', dest='install_cronjob',
                        help='Install cronjob for regular APKs update. Use --yes to automatically install to default locations')

    if len(sys.argv) < 2:
        sys.argv.append("-h")

    args = parser.parse_args()

    if args.version:
        print(__version__)
        return

    if args.install_cronjob:
        sys.exit(install_cronjob(args.yes_to_all))

    cli = GPlaycli(args, args.config)
    success, error = cli.connect_to_googleplay_api()
    if not success:
        logging.error("Cannot login to GooglePlay ( %s )" % error)
        sys.exit(ERRORS.CANNOT_LOGIN_GPLAY)

    if args.list:
        print(cli.list_folder_apks(args.list))

    if args.update_folder:
        cli.prepare_analyse_apks()

    if args.search_string:
        cli.verbose = True
        nb_results = 10
        if args.number_results:
            nb_results = args.number_results
        cli.search(list(), args.search_string, nb_results, not args.paid)

    if args.load_from_file:
        args.packages_to_download = load_from_file(args.load_from_file)

    if args.packages_to_download is not None:
        if args.dest_folder is not None:
            cli.set_download_folder(args.dest_folder[0])
        cli.download_packages(args.packages_to_download)


if __name__ == '__main__':
    main()
