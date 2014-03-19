# ostreepayload.py
# Deploy OSTree trees to target
#
# Copyright (C) 2012,2014  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
# Red Hat Author(s): Colin Walters <walters@redhat.com>
#

import shutil

from . import *


from pyanaconda.constants import *
from pyanaconda.flags import flags

from pyanaconda import iutil
from pyanaconda.i18n import _
from pyanaconda.progress import progressQ
from gi.repository import GLib
from gi.repository import Gio

from blivet.size import Size

import logging
log = logging.getLogger("anaconda")

from pyanaconda.errors import *
#from pyanaconda.progress import progress

class OSTreePayload(ArchivePayload):
    """ A OSTreePayload deploys a tree onto the target system. """
    def __init__(self, data):
        super(OSTreePayload, self).__init__(data)

    def setup(self, storage):
        super(OSTreePayload, self).setup(storage)

    @property
    def handlesBootloaderConfiguration(self):
        return True

    @property
    def kernelVersionList(self):
        # OSTree handles bootloader configuration
        return []

    @property
    def spaceRequired(self):
        # We don't have this data with OSTree at the moment
        return Size(500*1000*1000)

    def _safeExecWithRedirect(self, cmd, argv, **kwargs):
        """Like iutil.execWithRedirect, but treat errors as fatal"""
        rc = iutil.execWithRedirect(cmd, argv, **kwargs)
        if rc != 0:
            exn = PayloadInstallError("%s %s exited with code %d" % (cmd, argv, rc))
            if errorHandler.cb(exn) == ERROR_RAISE:
                raise exn

    def _pullProgressCb(self, asyncProgress):
        status = asyncProgress.get_status()
        outstanding_fetches = asyncProgress.get_uint('outstanding-fetches')
        if status:
            progressQ.send_message(status)
        elif outstanding_fetches > 0:
            bytes_transferred = asyncProgress.get_uint64('bytes-transferred')
            fetched = asyncProgress.get_uint('fetched')
            requested = asyncProgress.get_uint('requested')
            formatted_bytes = GLib.format_size_full(bytes_transferred, 0)

            if requested == 0:
                percent = 0.0
            else:
                percent = (fetched*1.0 / requested) * 100
            
            progressQ.send_message("Receiving objects: %d%% (%d/%d) %s" % (percent, fetched, requested, formatted_bytes))
        else:
            progressQ.send_message("Writing objects")

    def install(self):
        cancellable = None
        from gi.repository import OSTree
        ostreesetup = self.data.ostreesetup
        log.info("executing ostreesetup=%r" % (ostreesetup, ))

        # Initialize the filesystem - this will create the repo as well
        self._safeExecWithRedirect("ostree", ["admin", "--sysroot=" + ROOT_PATH,
                                              "init-fs", ROOT_PATH])

        repo_arg = "--repo=" + ROOT_PATH + '/ostree/repo'

        # Set up the chosen remote
        remote_args = [repo_arg, "remote", "add"]
        if ostreesetup.noGpg:
            remote_args.append("--set=gpg-verify=false")
        remote_args.extend([ostreesetup.remote,
                            ostreesetup.url])
        self._safeExecWithRedirect("ostree", remote_args)

        sysroot_path = Gio.File.new_for_path(ROOT_PATH)
        sysroot = OSTree.Sysroot.new(sysroot_path)
        sysroot.load(cancellable)

        repo = sysroot.get_repo(None)[1]
        progressQ.send_message(_("Starting pull of %s from %s") % (ostreesetup.ref, ostreesetup.remote))

        progress = OSTree.AsyncProgress.new()
        progress.connect('changed', self._pullProgressCb)
        repo.pull(ostreesetup.remote, [ostreesetup.ref], 0, progress, cancellable)

        self._safeExecWithRedirect("ostree", ["admin", "--sysroot=" + ROOT_PATH,
                                              "os-init", ostreesetup.osname])
            
        admin_deploy_args = ["admin", "--sysroot=" + ROOT_PATH,
                             "deploy", "--os=" + ostreesetup.osname]

        admin_deploy_args.append(ostreesetup.osname + ':' + ostreesetup.ref)

        log.info("ostree admin deploy starting")
        self._safeExecWithRedirect("ostree", admin_deploy_args)
        log.info("ostree admin deploy complete")

        ostree_sysroot_path = os.path.join(ROOT_PATH, 'ostree/deploy', ostreesetup.osname, 'current')
        iutil.setSysroot(ostree_sysroot_path)
        
    def postInstall(self):
        super(OSTreePayload, self).postInstall()
        
        bootmnt = self.storage.mountpoints.get('/boot')
        if bootmnt is not None:
            # Okay, now /boot goes back to the physical /, since
            # that's where ostree --sysroot expects to find it.  The
            # real fix for the madness is for tools like extlinux to
            # have a --sysroot option too.
            bootmnt.format.teardown()
            bootmnt.teardown()
            bootmnt.format.setup(bootmnt.format.options, chroot=ROOT_PATH)

        # Set up the sysroot bind mount after this so that we have the
        # tmp -> sysroot/tmp when executing %post scripts and the
        # like.
        self._safeExecWithRedirect("mount", ["--bind", ROOT_PATH, os.path.join(iutil.getSysroot(), 'sysroot')])

        # FIXME - Move extlinux.conf to syslinux.cfg, since that's
        # what OSTree knows about.
        sysroot_boot_extlinux = os.path.join(ROOT_PATH, 'boot/extlinux')
        sysroot_boot_syslinux = os.path.join(ROOT_PATH, 'boot/syslinux')
        sysroot_boot_loader = os.path.join(ROOT_PATH, 'boot/loader')
        if os.path.isdir(sysroot_boot_extlinux):
            assert os.path.isdir(sysroot_boot_loader)
            orig_extlinux_conf = os.path.join(sysroot_boot_extlinux, 'extlinux.conf')
            target_syslinux_cfg = os.path.join(sysroot_boot_loader, 'syslinux.cfg')
            log.info("Moving %s -> %s" % (orig_extlinux_conf, target_syslinux_cfg))
            os.rename(orig_extlinux_conf, target_syslinux_cfg)
            # A compatibility bit for OSTree
            os.mkdir(sysroot_boot_syslinux)
            os.symlink('../loader/syslinux.cfg', os.path.join(sysroot_boot_syslinux, 'syslinux.cfg'))
            # And *also* tell syslinux that the config is really in /boot/loader
            os.symlink('loader/syslinux.cfg', os.path.join(ROOT_PATH, 'boot/syslinux.cfg'))

        # FIXME - stub out firewalld
        firewalloffline = os.path.join(iutil.getSysroot(), 'usr/bin/firewall-offline-cmd')
        if not os.path.exists(firewalloffline):
            os.symlink('true', firewalloffline)

        # OSTree owns the bootloader configuration, so here we give it
        # the argument list we computed from storage, architecture and
        # such.
        set_kargs_args = ["admin", "--sysroot=" + ROOT_PATH,
                          "instutil", "set-kargs"]
        set_kargs_args.extend(self.storage.bootloader.boot_args)
        set_kargs_args.append("root=" + self.storage.rootDevice.fstabSpec)
        self._safeExecWithRedirect("ostree", set_kargs_args)
        # This command iterates over all files we might have created
        # and ensures they're labeled. It's like running
        # chroot(ROOT_PATH) + fixfiles, except with a better name and
        # semantics.
        self._safeExecWithRedirect("ostree", ["admin", "--sysroot=" + ROOT_PATH,
                                              "instutil", "selinux-ensure-labeled", ROOT_PATH, ""])
