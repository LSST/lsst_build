from __future__ import annotations

# Builder
import contextlib
import datetime
import json
import os
import requests
import select
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from typing import TextIO

import eups
import eups.tags
import yaml

from . import models
from .prepare import Manifest
from .prepare import agent_label


def product_representer(dumper, data):
    """Return YAML serialization."""
    obj = {
        "name": str(data.name),
        "sha1": str(data.sha1),
        "version": str(data.version),
    }
    return dumper.represent_mapping("tag:yaml.org,2002:map", obj)


yaml.add_representer(models.Product, product_representer)


def declare_eups_tag(tag, eups_obj):
    """Declare a new EUPS tag.

    FIXME: Not sure if this is the right way to programmatically
           define and persist a new tag. Ask RHL.
    """
    tags = eups_obj.tags
    if tag not in tags.getTagNames():
        tag = str(tag)
        tags.registerTag(tag)
        tags.saveGlobalTags(eups_obj.path[0])


class ProgressReporter:
    """Class that that displays the version string as a progress bar as an
    indicator of liveness.

    Parameters
    ----------
    out
        file this class will write the progress to
    product
        the product which we are reporting progress on
    """

    class ProductProgressReporter:
        """Progress bar helper class."""

        def __init__(self, out_file_obj: TextIO, product: models.Product):
            self.out = out_file_obj
            self.product = product

        def _build_started(self):
            self.out.write(f"{self.product.name:>20s}: ")
            self.out.flush()
            self.progress_bar = self.product.version + " "
            self.t0 = self.t = time.time()

        def report_progress(self):
            """Throttled progress reporting.

            Write out the version string as a progress bar, character by
            character, and then continue with dots.

            Throttle updates to one character every 2 seconds.
            """
            t1 = time.time()
            while self.t <= t1:
                if self.progress_bar:
                    self.out.write(self.progress_bar[0])
                    self.progress_bar = self.progress_bar[1:]
                else:
                    self.out.write(".")

                self.out.flush()
                self.t += 2

        def report_result(self, retcode, logfile):
            # Make sure we write out the full version string, even if the build
            # ended quickly.
            if self.progress_bar:
                self.out.write(self.progress_bar)
                self.out.flush()

            # If logfile is None, the product was already installed.
            if logfile is None:
                self.out.write("(already installed).\n")
                self.out.flush()
            else:
                elapsed_time = time.time() - self.t0
                if retcode:
                    print(f"ERROR ({elapsed_time:.1f} sec).", file=self.out)
                    print(f"*** error building product {self.product.name}.", file=self.out)
                    print(f"*** exit code = {retcode}", file=self.out)
                    print(f"*** log is in {logfile}", file=self.out)
                    print("*** last few lines:", file=self.out)

                    os.system(f"tail -n 10 {shlex.quote(logfile)} | sed -e 's/^/:::::  /'")
                else:
                    print(f"ok ({elapsed_time:.1f} sec).", file=self.out)
                self.out.flush()

            self.product = None

        def _finalize(self):
            # Usually called only when an exception is thrown.
            if self.product is not None:
                self.out.write("\n")
                self.out.flush()

    def __init__(self, out_file_obj):
        self.out = out_file_obj

    @contextlib.contextmanager
    def new_build(self, product):
        progress = ProgressReporter.ProductProgressReporter(self.out, product)
        progress._build_started()
        yield progress
        progress._finalize()


class Builder:
    """Class that builds and installs all products in a manifest.

    The result is tagged with the `Manifest`s build ID, if any.

    Parameters
    ----------
    build_dir
        the root directory of the build
    manifest
        the manifest we are building against
    progress
        the `ProgressReporter` reporting for this build
    eups
        an eups object for eups operations (e.g. discovering product info)
    """

    def __init__(self, build_dir: str, manifest: Manifest, progress: ProgressReporter, eups: eups.Eups):
        self.build_dir = build_dir
        self.manifest = manifest
        self.progress = progress
        self.eups = eups
        self.built: list[models.Product] = []
        self.failed_at = None
        self.check_run_id = None # Store GH check run ID

    def _tag_product(self, name, version, tag):
        if tag:
            self.eups.declare(name, version, tag=str(tag))

    def _build_product(self, product, progress):
        # run the eupspkg sequence for the product
        #
        productdir = os.path.abspath(os.path.join(self.build_dir, product.name))
        buildscript = os.path.join(productdir, "_build.sh")
        logfile = os.path.join(productdir, "_build.log")
        eupsdir = eups.productDir("eups")
        eupspath = os.environ["EUPS_PATH"]

        # construct the tags file with exact dependencies
        setups = [
            f"\t{dep.name:20s} {dep.version}"
            for dep in self.manifest.product_index.flat_dependencies(product)
        ]

        # create the buildscript
        with open(buildscript, "w", encoding="utf-8") as fp:
            text = textwrap.dedent(
                """\
            #!/bin/bash

            # redirect stderr to stdin
            exec 2>&1

            # stop on any error
            set -ex

            # define the setup command, but preserve EUPS_PATH
            . "{eupsdir}/bin/setups.sh"
            export EUPS_PATH="{eupspath}"

            cd "{productdir}"

            # clean up the working directory
            git reset --hard
            git clean -d -f -q -x -e '_build.*'

            # prepare
            eupspkg PRODUCT={product} VERSION={version} FLAVOR=generic prep

            # setup the package with its exact dependencies
            cat > _build.tags <<-EOF
            {setups}
            EOF
            set +x
            echo "Setting up environment with EUPS"
            setup --vro=_build.tags -r .
            set -x

            # build
            eupspkg PRODUCT={product} VERSION={version} FLAVOR=generic config
            eupspkg PRODUCT={product} VERSION={version} FLAVOR=generic build
            if [ -d  tests/.tests ] && \
                [ "`ls tests/.tests/*.failed 2> /dev/null | wc -l`" -ne 0 ]; then
                echo "*** Failed unit tests.";
                exit 1
            fi
            eupspkg PRODUCT={product} VERSION={version} FLAVOR=generic install

            # declare to EUPS
            eupspkg PRODUCT={product} VERSION={version} FLAVOR=generic decl

            # explicitly append SHA1 to pkginfo
            echo SHA1={sha1} >> $(eups list {product} {version} -d)/ups/pkginfo
            """.format(
                    product=product.name,
                    version=product.version,
                    sha1=product.sha1,
                    productdir=productdir,
                    setups="\n            ".join(setups),
                    eupsdir=eupsdir,
                    eupspath=eupspath,
                )
            )

            fp.write(text)

        # Make executable (equivalent of 'chmod +x $buildscript')
        st = os.stat(buildscript)
        os.chmod(buildscript, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Run the build script
        with open(logfile, "w", encoding="utf-8") as logfp:
            # execute the build file from the product directory, capturing the
            # output and return code.
            process = subprocess.Popen(
                buildscript, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=productdir
            )
            select_list = [process.stdout]
            buf = b""
            while True:
                # Wait up to 2 seconds for output
                ready_to_read, _, _ = select.select(select_list, [], [], 2)
                if ready_to_read:
                    c = process.stdout.read(1)
                    buf += c
                    if (c == b"" or c == b"\n") and buf:
                        line = f"[{datetime.datetime.utcnow().isoformat()}Z] {buf.decode()}"
                        logfp.write(line)
                        buf = b""
                    # Ready to read but nothing there means end of file
                    if c == b"":
                        break
                progress.report_progress()

        retcode = process.poll()
        if not retcode:
            # copy the log file to product directory
            eups_prod = self.eups.getProduct(product.name, product.version)
            shutil.copy2(logfile, eups_prod.dir)
        else:
            eups_prod = None

        return (eups_prod, retcode, logfile)

    def _build_product_if_needed(self, product):
        # Build a product if it hasn't been installed already
        #
        with self.progress.new_build(product) as progress:
            try:
                # skip the build if the product has been installed
                eups_prod, retcode, logfile = self.eups.getProduct(product.name, product.version), 0, None
            except eups.ProductNotFound:
                eups_prod, retcode, logfile = self._build_product(product, progress)

            if eups_prod is not None and self.manifest.build_id not in eups_prod.tags:
                self._tag_product(product.name, product.version, self.manifest.build_id)

            progress.report_result(retcode, logfile)

        return retcode == 0

    def build(self):
        # Make sure EUPS knows about the build_id tag
        if self.manifest.build_id:
            declare_eups_tag(self.manifest.build_id, self.eups)

        # Build all products
        for product in self.manifest.product_index.values():
            if not self._build_product_if_needed(product):
                self.failed_at = product
                return False
            self.built.append(product)
        return True # Returns true on success, to use for check
    
    def rm_status(self):
        if os.path.isfile(self.status_file()):
            os.remove(self.status_file())

    def status_file(self):
        return os.path.join(self.build_dir, "status.yaml")

    def write_status(self):
        status = {
            "built": self.built,
        }

        if self.failed_at is not None:
            status["failed_at"] = self.failed_at

        with open(self.status_file(), "w", encoding="utf-8") as sf:
            yaml.dump(status, sf, encoding="utf-8", default_flow_style=False)

    @staticmethod
    def run(args):
        # Call agent() from prepare.py
        agent = agent_label()
        
        # Ensure build directory exists and is writable
        build_dir = args.build_dir
        if not os.access(build_dir, os.W_OK):
            raise Exception(f"Directory {build_dir!r} does not exist or isn't writable.")

        # Build products
        eups_obj = eups.Eups()

        progress = ProgressReporter(sys.stdout)

        manifest_fn = os.path.join(build_dir, "manifest.txt")
        with open(manifest_fn, encoding="utf-8") as fp:
            manifest = Manifest.from_file(fp)

        # Load PR information saved by prepare.py
        pr_info = Builder.load_pr_info(build_dir)
        print(f"this is the pr_info {pr_info}")

        b = Builder(build_dir, manifest, progress, eups_obj)
        b.rm_status()

        # Post "build pending" status to GitHub
        if pr_info and agent != "error":
            description = f"Build started on {agent}"
            print("Github status pending")
            Builder.post_github_status(pr_info, state='pending', description=description, agent=agent)

        retcode = b.build()

        # Post "build succeeded" or "build failed" status to GitHub
        if pr_info and agent != "error":
            description = f"Build {'succeeded' if retcode else 'failed'} on {agent}"
            state = "success" if retcode else "failure"
            Builder.post_github_status(pr_info, state=state, description=description, agent=agent)

        b.write_status()
        sys.exit(0 if retcode else 1)



    @staticmethod
    def load_pr_info(build_dir):
        """Load PR information saved by prepare.py."""

        pr_info_file = os.path.join(build_dir, 'pr_info.json')
        if os.path.exists(pr_info_file):
            with open(pr_info_file, 'r', encoding='utf-8') as f:
                pr_info = json.load(f)
            return pr_info
        else:
            return None
        
    # def authenticate_github_app(self):
    #     """Authenticate as GitHub App and obtain installation access token."""

    #     app_id = os.environ.get('GITHUB_APP_ID')
    #     private_key = os.environ.get('GITHUB_APP_KEY')
    #     installation_id = os.environ.get('GITHUB_APP_INSTALLATION_ID')

    #     if not all([app_id, private_key, installation_id]):
    #         print("GitHub App credentials not found in environment variables.")
    #         return None

    #     # Generate JWT
    #     payload = {
    #         'iat': int(time.time()),
    #         'exp': int(time.time()) + (10 * 60),
    #         'iss': app_id
    #     }

    #     jwt_token = jwt.encode(payload, private_key, algorithm='RS256')
    #     self.jwt_token = jwt_token

    #     # Get Installation Access Token
    #     headers = {
    #         'Authorization': f'Bearer {jwt_token}',
    #         'Accept': 'application/vnd.github.v3+json'
    #     }

    #     url = f'https://api.github.com/app/installations/{installation_id}/access_tokens'
    #     response = requests.post(url, headers=headers)
    #     if response.status_code == 201:
    #         access_token = response.json()['token']
    #         self.access_token = access_token
    #         return access_token
    #     else:
    #         print(f'Failed to get installation access token: {response.status_code} - {response.text}')
    #         return None
    
    # def get_installation_id(self, owner, repo):
    #     headers = {
    #         'Authorization': f'Bearer {self.jwt_token}',
    #         'Accept': 'application/vnd.github.v3+json'
    #     }

    #     url = f'https://api.github.com/repos/{owner}/{repo}/installation'
    #     response = requests.get(url, headers=headers)
    #     if response.status_code == 200:
    #         installation_id = response.json()['id']
    #         self.installation_id = installation_id
    #         return installation_id
    #     else:
    #         print(f'Failed to get installation ID: {response.status_code} - {response.text}')
    #         return None

    # def create_github_check_run(self, pr_info):
    #     """Create a check run on GitHub to indicate the build has started."""

    #     print("Creating GitHub check run for build start.")
    #     if not self.access_token:
    #         print("There is a problem retrieving the installation token for Github authentication.")
    #         return

    #     owner = pr_info['owner']
    #     repo = pr_info['repo']
    #     sha = pr_info['sha']

    #     url = f"https://api.github.com/repos/{owner}/{repo}/check-runs"
    #     headers = {
    #         'Authorization': f'token {self.access_token}',
    #         'Accept': 'application/vnd.github.v3+json'
    #     }

    #     data = {
    #         'name': 'Jenkins Check',
    #         'head_sha': sha,
    #         'status': 'in_progress',
    #         'started_at': datetime.datetime.utcnow().isoformat() + 'Z'
    #     }

    #     response = requests.post(url, headers=headers, json=data)
    #     if response.status_code == 201:
    #         check_run = response.json()
    #         self.check_run_id = check_run['id']
    #         print(f"GitHub check run created with ID {self.check_run_id}.")
    #     else:
    #         print(f"Failed to create GitHub check run: {response.status_code} - {response.text}")

    # def update_github_check_run(self, pr_info, success):
    #     """Update the GitHub check run with the build result."""
        
    #     if self.check_run_id is None:
    #         print("No check run ID available to update.")
    #         return

    #     if not self.access_token:
    #         print("No access_token. Make sure create_github_check run is called and running properly")
    #         return

    #     print("Updating GitHub check run with build result.")

    #     owner = pr_info['owner']
    #     repo = pr_info['repo']
    #     sha = pr_info['sha']

    #     url = f"https://api.github.com/repos/{owner}/{repo}/check-runs/{self.check_run_id}"
    #     headers = {
    #         'Authorization': f'token {self.access_token}',
    #         'Accept': 'application/vnd.github.v3+json'
    #     }

    #     conclusion = 'success' if success else 'failure'
    #     data = {
    #         'name': 'Jenkins Check',
    #         'status': 'completed',
    #         'completed_at': datetime.datetime.utcnow().isoformat() + 'Z',
    #         'conclusion': conclusion,
    #         'output': {
    #             'title': 'Build Result',
    #             'summary': 'Build succeeded.' if success else 'Build failed.'
    #         }
    #     }

    #     response = requests.patch(url, headers=headers, json=data)
    #     if response.status_code == 200:
    #         print("GitHub check run updated successfully.")
    #     else:
    #         print(f"Failed to update GitHub check run: {response.status_code} - {response.text}")



    @staticmethod
    def post_github_status(pr_info, state, description, agent):
        """Post a status to the matching PR on GitHub.

        Parameters
        ----------
        pr_info : dict
            Dictionary containing 'owner', 'repo', 'pr_number', 'sha'.
        state : str
            The state of the status ('pending', 'success', 'failure', or 'error').
        description : str
            A short description of the status.
        """
        print(f"Posting GitHub status: {state} - {description}")
        token = os.environ['GITHUB_TOKEN']
        if not token:
            print("GITHUB_TOKEN not found in environment variables.")
            return

        owner = pr_info['owner']
        repo = pr_info['repo']
        sha = pr_info['sha']  # The commit SHA to which the status will be attached

        url = f"https://api.github.com/repos/{owner}/{repo}/statuses/{sha}"
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        build_url = os.environ['BUILD_URL']
        if build_url is None:
            build_url = "https://rubin-ci-dev.slac.stanford.edu/blue/organizations/jenkins/stack-os-matrix/activity"

        data = {
            'state': state,
            'description': description,
            'context': f'Jenkins Build ({agent})',
            'target_url': build_url
        }

        response = requests.post(url, headers=headers, json=data)
        if response.status_code == 201:
            print("GitHub status posted successfully.")
        else:
            print(f"Failed to post GitHub status: {response.status_code} - {response.text}")