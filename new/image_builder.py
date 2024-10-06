#!/usr/bin/env python3
import argparse
from contextlib import closing
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import logging
import os
from shlex import quote
import shutil
import socket
from threading import Thread
from time import monotonic

import netifaces

from lib.git import Git
from lib.helpers import setup_logging, execute, quote_all, rmtree, refuse_root


class ImageBuilder:
    version_mapping = {
        "sagitta": "1.4.x",
        "circinus": "1.5.x",
    }

    def __init__(self, branch, vyos_build_git, vyos_mirror, extra_options, flavor, build_by, version, bind_addr,
                 bind_port, keep_build):
        self.branch = branch
        self.vyos_build_git = vyos_build_git
        self.vyos_mirror = vyos_mirror
        self.extra_options = extra_options
        self.flavor = flavor
        self.build_by = build_by
        self.version = version
        self.bind_addr = bind_addr
        self.bind_port = bind_port
        self.keep_build = keep_build

        self.project_dir: str = os.path.realpath(os.path.dirname(__file__))
        self.cwd = os.getcwd()

    def build(self):
        begin = monotonic()
        if self.vyos_mirror == "local":
            vyos_mirror = self.start_local_webserver()
            logging.info("Starting local APT repository at %s" % vyos_mirror)
        else:
            vyos_mirror = self.vyos_mirror
            logging.info("Using supplied APT repository at %s" % vyos_mirror)

        logging.info("Pulling vyos-build docker image")
        docker_image = "vyos/vyos-build:%s" % self.branch
        execute("docker pull %s" % quote_all(docker_image), passthrough=True)

        vyos_build_repo = os.path.join(self.project_dir, "build", "%s-image-build" % self.branch)
        git = Git(vyos_build_repo)
        if not self.keep_build:
            if git.exists():
                rmtree(vyos_build_repo)

        if not git.exists():
            git.clone(self.vyos_build_git, self.branch)

        # TODO: remove me, temporary hack until vyos-build is fixed
        with open(os.path.join(git.repo_path, "data/build-flavors/generic.toml"), "r+") as file:
            contents = file.read()
            contents = contents.replace("vyos-xe-guest-utilities", "xen-guest-agent")
            file.seek(0)
            file.write(contents)
            file.truncate()

        version = self.version
        if version == "auto":
            if self.branch in self.version_mapping:
                version = self.version_mapping[self.branch]
            else:
                version = self.branch

        # build image
        build_image_pieces = [
            "sudo --preserve-env ./build-vyos-image",
            quote(self.flavor),
            "--architecture", quote("amd64"),
            "--build-by", quote(self.build_by),
            "--build-type", quote("release"),
            "--version", quote(version),
            "--vyos-mirror", quote(vyos_mirror),
        ]
        if self.vyos_mirror == "local":
            build_image_pieces.extend([
                "--custom-apt-key", quote("/opt/apt.gpg.key"),
            ])
        if self.extra_options:
            build_image_pieces.append(self.extra_options)
        build_image_command = " ".join(build_image_pieces)

        # docker run
        docker_pieces = [
            "docker run --rm -it",
            "-v %s:/vyos" % quote(vyos_build_repo),
        ]
        if self.vyos_mirror == "local":
            apt_key_path = os.path.join(self.project_dir, "apt", "apt.gpg.key")
            docker_pieces.extend([
                "-v %s:/opt/apt.gpg.key" % quote(apt_key_path),
            ])
        docker_pieces.extend([
            "-w /vyos --privileged --sysctl net.ipv6.conf.lo.disable_ipv6=0",
            "-e GOSU_UID=%s -e GOSU_GID=%s" % (os.getuid(), os.getgid()),
            docker_image,
            build_image_command,
        ])
        docker_command = " ".join(docker_pieces)

        logging.info("Using build image command: '%s'" % build_image_command)
        logging.info("Using docker run command: '%s'" % docker_command)
        logging.info("Executing image build now...")

        execute(docker_command, passthrough=True)

        image_path = None
        build_dir = os.path.join(vyos_build_repo, "build")
        if os.path.exists(build_dir):
            for entry in os.scandir(build_dir):
                if version in entry.name and entry.name.endswith(".iso"):
                    image_path = entry.path
                    break

        if image_path is None:
            image_path = os.path.join(build_dir, "live-image-amd64.hybrid.iso")

        if not os.path.exists(image_path):
            logging.error("Image not found, see previous log for hints, inspect build here: %s" % build_dir)
            exit(1)

        new_image_path = os.path.join(self.cwd, os.path.basename(image_path))
        if image_path != new_image_path:
            shutil.copy2(image_path, new_image_path)

        elapsed = round(monotonic() - begin, 3)
        logging.info("Done in %s seconds, image is available here: %s" % (elapsed, new_image_path))

    def start_local_webserver(self):
        address = self.get_local_ip() if not self.bind_addr else self.bind_addr
        port = self.get_free_port(address) if not self.bind_port else self.bind_port
        thread = Thread(target=self.serve_webserver, args=(address, port), name="LocalWebServer", daemon=True)
        thread.start()
        return "http://%s:%s/%s" % (address, "" if port == 80 else port, self.branch)

    def serve_webserver(self, address, port):
        os.chdir(os.path.join(self.project_dir, "apt"))
        # noinspection PyTypeChecker
        server = ThreadingHTTPServer((address, port), WebServerHandler)
        server.serve_forever()

    def get_free_port(self, address):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind((address, 0))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return sock.getsockname()[1]

    def get_local_ip(self):
        selected_address = None
        docker_address = None
        for interface in netifaces.interfaces():
            if interface.startswith("lo"):
                continue

            addresses = netifaces.ifaddresses(interface)
            if netifaces.AF_INET not in addresses:
                continue

            for address in addresses[netifaces.AF_INET]:
                if "addr" not in address or not address["addr"]:
                    continue

                selected_address = address["addr"]
                if interface == "docker0":
                    docker_address = selected_address

        if docker_address:
            selected_address = docker_address

        if selected_address is None:
            raise Exception(
                "Unable to find local address, please specify local IP (NOT localhost) via --bind-addr option"
            )

        return selected_address


class WebServerHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    setup_logging()

    try:
        refuse_root()

        parser = argparse.ArgumentParser()
        parser.add_argument("branch", help="VyOS branch (current, circinus)")
        parser.add_argument("--vyos-build-git", default="https://github.com/vyos/vyos-build.git",
                            help="Git URL of vyos-build")
        parser.add_argument("--vyos-mirror", default="local", help="VyOS package repository (URL or 'local')")
        parser.add_argument("--extra-options", help="Extra options for the build-vyos-image")
        parser.add_argument("--flavor", default="generic")
        parser.add_argument("--build-by", default="myself@localhost")
        parser.add_argument("--version", default="auto")
        parser.add_argument("--bind-addr", help="Bind local webserver to static address instead of automatic")
        parser.add_argument("--bind-port", type=int, help="Bind local webserver to static port instead of random")
        parser.add_argument("--keep-build", action="store_true", help="Keep previous vyos-build repository")
        args = parser.parse_args()

        builder = ImageBuilder(**vars(args))
        builder.build()

    except KeyboardInterrupt:
        exit(1)
    except Exception as e:
        logging.exception(e)
        exit(1)