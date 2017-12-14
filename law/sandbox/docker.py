# -*- coding: utf-8 -*-

"""
Docker sandbox implementation.
"""


__all__ = ["DockerSandbox"]


import os
from collections import OrderedDict
from fnmatch import fnmatch
from subprocess import PIPE, STDOUT
from uuid import uuid4

import luigi
import six

import law
from law.sandbox.base import Sandbox
from law.config import Config
from law.util import law_base, make_list, tmp_file, interruptable_popen


class DockerSandbox(Sandbox):

    sandbox_type = "docker"

    default_docker_args = ["--rm"]

    # env cache per image
    _envs = {}

    @property
    def image(self):
        return self.name

    @property
    def env(self):
        # strategy: create a tempfile, forward it to a container, let python dump its full env,
        # close the container and load the env file
        if self.image not in self._envs:
            with tmp_file() as tmp:
                tmp_path = os.path.realpath(tmp[1])
                env_path = os.path.join("/tmp", str(hash(tmp_path))[-8:])

                cmd = "docker run --rm -v {0}:{1} riga/law_example_base python -c \"" \
                    "import os,pickle;pickle.dump(os.environ,open('{1}','w'))\""
                cmd = cmd.format(tmp_path, env_path)

                returncode, out, _ = interruptable_popen(cmd, shell=True, executable="/bin/bash",
                    stdout=PIPE, stderr=STDOUT)
                if returncode != 0:
                    raise Exception("docker sandbox env loading failed: " + str(out))

                with open(tmp_path, "r") as f:
                    env = six.moves.cPickle.load(f)

            # add env variables defined in the config
            env.update(self.get_config_env())

            # add env variables defined by the task
            env.update(self.get_task_env())

            # cache
            self._envs[self.image] = env

        return self._envs[self.image]

    def cmd(self, proxy_cmd):
        cfg = Config.instance()

        # get args for the docker command as configured in the task
        # TODO: this looks pretty random
        docker_args = make_list(getattr(self.task, "docker_args", self.default_docker_args))

        # container name
        docker_args.append("--name '{}_{}'".format(self.task.task_id, str(uuid4())[:8]))

        # helper to build forwarded paths
        section = "docker_" + self.image
        section = section if cfg.has_section(section) else "docker"
        forward_dir = cfg.get(section, "forward_dir")
        python_dir = cfg.get(section, "python_dir")
        bin_dir = cfg.get(section, "bin_dir")
        stagein_dir = cfg.get(section, "stagein_dir")
        stageout_dir = cfg.get(section, "stageout_dir")
        def dst(*args):
            return os.path.join(forward_dir, *(str(arg) for arg in args))

        # helper for adding a volume
        def add_vol(*vol):
            src = vol[0]
            docker_args.extend(["-v", ":".join(vol)])
            # ensure that source directories exist
            if not os.path.isfile(src) and not os.path.exists(src):
                os.makedirs(src)

        # environment variables to set
        env = OrderedDict()

        # sandboxing variables
        env["LAW_SANDBOX"] = self.key
        env["LAW_SANDBOX_SWITCHED"] = "1"
        if not self.use_local_scheduler:
            env["LAW_SANDBOX_WORKER_ID"] = "{}".format(self.task.worker_id)
        if self.stagein_info:
            env["LAW_SANDBOX_STAGEIN_DIR"] = "{}".format(dst(stagein_dir))
            add_vol(self.stagein_info.stage_dir.path, dst(stagein_dir))
        if self.stageout_info:
            env["LAW_SANDBOX_STAGEOUT_DIR"] = "{}".format(dst(stageout_dir))
            add_vol(self.stageout_info.stage_dir.path, dst(stageout_dir))

        # prevent python from writing byte code files
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        # adjust path variables
        env["PATH"] = os.pathsep.join(["$PATH", dst("bin"), dst(python_dir, "law", "scripts")])
        env["PYTHONPATH"] = os.pathsep.join(["$PYTHONPATH", dst(python_dir)])

        # forward python directories of law and dependencies
        for mod in (law, luigi, six):
            path = mod.__file__
            dirname = os.path.dirname(path)
            name, ext = os.path.splitext(os.path.basename(path))
            if name == "__init__":
                vsrc = dirname
                vdst = dst(python_dir, os.path.basename(dirname))
            else:
                vsrc = os.path.join(dirname, name) + ".py"
                vdst = dst(python_dir, name) + ".py"
            add_vol(vsrc, vdst)

        # forward the luigi config file
        for p in luigi.configuration.LuigiConfigParser._config_paths[::-1]:
            if os.path.exists(p):
                add_vol(p, dst("luigi.cfg"))
                env["LUIGI_CONFIG_PATH"] = dst("luigi.cfg")
                break

        # add env variables defined in the config and by the task
        env.update(self.get_config_env())
        env.update(self.get_task_env())

        # forward volumes defined in the config and by the task
        vols = {}
        vols.update(self.get_config_volumes())
        vols.update(self.get_task_volumes())
        vol_mapping = {"${PY}": dst(python_dir), "${BIN}": dst(bin_dir)}
        for hdir, cdir in six.iteritems(vols):
            if not cdir:
                add_vol(hdir)
            else:
                cdir = cdir.replace("${PY}", dst(python_dir)).replace("${BIN}", dst(bin_dir))
                add_vol(hdir, cdir)

        # build commands to add env variables
        pre_cmds = []
        for tpl in env.items():
            pre_cmds.append("export {}=\"{}\"".format(*tpl))

        # build the final command which may run as a certain user
        sandbox_user = self.task.sandbox_user
        if sandbox_user:
            if not isinstance(sandbox_user, (tuple, list)) or len(sandbox_user) != 2:
                raise Exception("sandbox_user must return 2-tuple")
            docker_args.append("-u={}:{}".format(*sandbox_user))

        cmd = "docker run {docker_args} {image} bash -l -c '{pre_cmd}; {proxy_cmd}'".format(
            proxy_cmd=proxy_cmd, pre_cmd="; ".join(pre_cmds), image=self.image,
            docker_args=" ".join(docker_args))

        return cmd

    def get_config_env(self):
        cfg = Config.instance()
        env = {}

        section = "docker_env_" + self.image
        section = section if cfg.has_section(section) else "docker_env"

        for name, value in cfg.items(section):
            if "*" in name or "?" in name:
                names = [key for key in os.environ.keys() if fnmatch(key, name)]
            else:
                names = [name]
            for name in names:
                env[name] = value if value is not None else os.environ.get(name, "")

        return env

    def get_config_volumes(self):
        cfg = Config.instance()
        vols = {}

        section = "docker_volumes_" + self.image
        section = section if cfg.has_section(section) else "docker_volumes"

        for hdir, cdir in cfg.items(section):
            vols[os.path.expandvars(os.path.expanduser(hdir))] = cdir

        return vols

    def get_task_env(self):
        task_env_getter = getattr(self.task, "get_docker_env", None)
        if callable(task_env_getter):
            return task_env_getter(self.image)
        else:
            return {}

    def get_task_volumes(self):
        task_vol_getter = getattr(self.task, "get_docker_volumes", None)
        if callable(task_vol_getter):
            return task_vol_getter(self.image)
        else:
            return {}
