""" IPA AD Trust Sanity tests

:requirement: IDM-SSSD-REQ: Testing SSSD in IPA Provider
:casecomponent: sssd
:subsystemteam: sst_idm_sssd
:upstream: yes
"""

import random
import re
import time
import pytest
import paramiko
from sssd.testlib.common.utils import sssdTools
from sssd.testlib.common.utils import SSHClient
from sssd.testlib.common.utils import ADOperations


@pytest.mark.usefixtures('setup_ipa_client')
@pytest.mark.tier2
@pytest.mark.trust
class TestADTrust(object):
    """ IPA AD Trust tests """
    def test_basic_sssctl_list(self, multihost):
        """
        :title: Verify sssctl lists trusted domain
        :id: 8da8919d-524c-4498-8dc8-608eb5e139b0
        """
        domain_list = 'sssctl domain-list'
        ad_domain_name = multihost.ad[0].domainname
        cmd = multihost.master[0].run_command(domain_list, raiseonerr=False)
        mylist = cmd.stdout_text.split()
        assert ad_domain_name in mylist

    def test_pam_sss_gss_handle_large_krb_ticket(self, multihost,
                                                 create_aduser_group):
        """
        :title: Verify pam_sss_gss.so can handle large kerberos ticket
                for sudo
        :id: 456ea53b-6702-4b8e-beb1-eee841b85fed
        :bugzilla: https://bugzilla.redhat.com/show_bug.cgi?id=1948657
        :steps:
         1. Add sudo rule in IPA-server for AD-users
         2. Modify /etc/krb5.conf.d/kcm_default_ccache to specify location
            of storing a TGT
         3. Enable pam_sss_gss.so for auth in /etc/pam.d/{sudo,sudo-i} files
         4. Add a sudo rule for AD-user
         5. Log in on ipa-client as AD-user
         6. Run kinit and fetch tgt
         7. Run sudo command
         8. Remove sudo cache
         9. Run sudo command again
        :expectedresults:
         1. Should succeed
         2. Should succeed
         3. Should succeed
         4. Should succeed
         5. Should succeed
         6. Should succeed
         7. Should not ask password, and should succeed
         8. Should succeed
         9. Should not ask password, and should succeed

        """
        (aduser, adgroup) = create_aduser_group
        ad_dmn_name = multihost.ad[0].domainname
        fq_aduser = f'{aduser}@{ad_dmn_name}'
        client = sssdTools(multihost.client[0], multihost.ad[0])
        ipaserver = sssdTools(multihost.master[0])
        cmd = 'dnf install -y sssd sssd-kcm'
        multihost.client[0].run_command(cmd, raiseonerr=False)
        domain_name = ipaserver.get_domain_section_name()
        domain_section = 'domain/{}'.format(domain_name)
        params = {'pam_gssapi_services': 'sudo, sudo-i'}
        client.sssd_conf(domain_section, params)
        krbkcm = '/etc/krb5.conf.d/kcm_default_ccache'
        bk_krbkcm = '/tmp/kcm_default_ccache'
        multihost.client[0].run_command(f'cp {krbkcm} {bk_krbkcm}')
        cmd = "echo -e  '[libdefaults]\n' \
              '    default_ccache_name  = FILE:/tmp/krb5cc_%{uid}:'"
        multihost.client[0].run_command(cmd, raiseonerr=False)
        multihost.client[0].service_sssd('restart')
        pam_sss_gss = "auth       sufficient   pam_sss_gss.so debug"
        for pam_file in "/etc/pam.d/sudo-i", "/etc/pam.d/sudo":
            cmd = f'sed -i "1 i {pam_sss_gss}" {pam_file}'
            multihost.client[0].run_command(cmd, raiseonerr=False)
        cmd = f'echo "{fq_aduser} ALL=(ALL) ALL" >> /etc/sudoers'
        multihost.client[0].run_command(cmd, raiseonerr=False)
        log = re.compile(f'.*System.*error.*Broken.*pipe.*')
        try:
            ssh = SSHClient(multihost.client[0].ip,
                            username=f'{fq_aduser}',
                            password='Secret123')
        except paramiko.ssh_exception.AuthenticationException:
            pytest.fail(f'{aduser} failed to login')
        else:
            (_, _, exit_status) = ssh.execute_cmd(f'kinit {fq_aduser}',
                                                  stdin='Secret123')
            assert exit_status == 0
            (stdout, _, exit_status) = ssh.execute_cmd('sudo -l')
            assert exit_status == 0
            otpt = stdout.readlines()
            for line in otpt:
                res = log.search(line)
                assert res is None
            (stdout, _, exit_status) = ssh.execute_cmd('sudo id')
            assert exit_status == 0
            (stdout, _, exit_status) = ssh.execute_cmd('sudo -k')
            assert exit_status == 0
        client.sssd_conf(domain_section, params, action='delete')
        for pam_file in "/etc/pam.d/sudo-i", "/etc/pam.d/sudo":
            cmd = f'sed -i "1d" {pam_file}'
            multihost.client[0].run_command(cmd, raiseonerr=False)
        cmd = f'sed -i "$ d" /etc/sudoers'
        multihost.client[0].run_command(cmd, raiseonerr=False)
        cmd = f'mv {bk_krbkcm} {krbkcm}'
        multihost.client[0].run_command(cmd, raiseonerr=False)

    def test_ipaserver_sss_cache_user(self, multihost):
        """
        :title: Verify AD user is cached on IPA server
         when ipa client queries AD User
        :id: 4a48ee7a-62d1-4eea-9f33-7df3fccc908e
        """
        ipaserver = sssdTools(multihost.master[0])
        domain_name = ipaserver.get_domain_section_name()
        domain_section = 'domain/{}'.format(domain_name)
        cache_path = '/var/lib/sss/db/cache_%s.ldb' % domain_name
        ad_domain_name = multihost.ad[0].domainname
        user_name = 'Administrator@%s' % (ad_domain_name)
        id_cmd = 'id %s' % user_name
        multihost.master[0].run_command(id_cmd, raiseonerr=False)
        multihost.client[0].run_command(id_cmd, raiseonerr=False)
        dn = 'name=Administrator@%s,cn=users,cn=%s,cn=sysdb' % (ad_domain_name,
                                                                ad_domain_name)
        ldb_cmd = 'ldbsearch -H %s -b "%s"' % (cache_path, dn)
        multihost.master[0].run_command(ldb_cmd, raiseonerr=False)

    def test_enforce_gid(self, multihost):
        """
        :title: Verify whether the new gid is enforceable when
         gid of AD Group Domain Users is overridden
        :id: 3581c7c0-d598-4e34-bb9b-9d791b93ec65
        :bugzilla:
         https://bugzilla.redhat.com/show_bug.cgi?id=1817219
        """
        create_view = 'ipa idview-add  foo_bar'
        multihost.master[0].run_command(create_view)
        ad_domain_name = multihost.ad[0].domainname
        ad_grp = 'Domain Users@%s' % ad_domain_name
        cmd = 'ipa idoverridegroup-add foo_bar "%s" --gid=40000000' % (ad_grp)
        multihost.master[0].run_command(cmd, raiseonerr=False)
        # apply the view on client
        client_hostname = multihost.client[0].sys_hostname
        apply_view = "ipa idview-apply foo_bar --hosts=%s" % client_hostname
        multihost.master[0].run_command(apply_view)
        client = sssdTools(multihost.client[0])
        client.clear_sssd_cache()
        time.sleep(5)
        user_name = 'Administrator@%s' % (ad_domain_name)
        id_cmd = 'id %s' % user_name
        cmd = multihost.client[0].run_command(id_cmd, raiseonerr=False)
        group = "40000000(domain users@%s)" % ad_domain_name
        delete_id_view = 'ipa idview-del foo_bar'
        multihost.master[0].run_command(delete_id_view)
        client.clear_sssd_cache()
        assert group in cmd.stdout_text

    def test_honour_idoverride(self, multihost, create_aduser_group):
        """
        :title: Verify sssd honours the customized ID View
        :id: 0c0dcfbb-6099-4c61-81c9-3bd3a003ff58
        :bugzilla:
         https://bugzilla.redhat.com/show_bug.cgi?id=1826720
        """
        (aduser, adgroup) = create_aduser_group
        domain = multihost.ad[0].domainname
        ipa_client = sssdTools(multihost.client[0])
        ipa_client.clear_sssd_cache()
        ad_user_fqdn = '%s@%s' % (aduser, domain)
        id_cmd = 'id -g %s' % (ad_user_fqdn)
        cmd = multihost.master[0].run_command(id_cmd, raiseonerr=False)
        current_gid = cmd.stdout_text.strip()
        create_view = 'ipa idview-add madrid_trust_view'
        multihost.master[0].run_command(create_view)
        cmd = 'ipa idoverrideuser-add madrid_trust_view '\
              '%s --uid=50001 --gidnumber=50000 '\
              '--home=/home/%s' % (ad_user_fqdn, aduser)
        multihost.master[0].run_command(cmd, raiseonerr=False)
        # apply the view on client
        apply_view = "ipa idview-apply madrid_trust_view "\
                     "--hosts=%s" % multihost.client[0].sys_hostname
        multihost.master[0].run_command(apply_view)
        ipa_client.clear_sssd_cache()
        time.sleep(5)
        id_cmd = 'id %s' % ad_user_fqdn
        count = 0
        for i in range(50):
            cmd = multihost.client[0].run_command(id_cmd, raiseonerr=False)
            gid = cmd.stdout_text.strip()
            if gid == current_gid:
                count += 1
        delete_id_view = 'ipa idview-del madrid_trust_view'
        multihost.master[0].run_command(delete_id_view)
        ipa_client.clear_sssd_cache()
        assert count == 0

    def test_ipa_missing_secondary_ipa_posix_groups(self, multihost,
                                                    create_aduser_group):
        """
        :title: IPA missing secondary IPA Posix groups in latest sssd
        :id: bbb82516-4127-4053-9b06-9104ac889819
        :setup:
         1. Configure trust between IPA server and AD.
         2. Configure client machine with SSSD integrated to IPA.
         3. domain-resolution-order set so the AD domains are checked first
         4. Create external group that is member of a posix group
         5. Create user that is a member of the external group
         6. Make sure that external group is member of posix group.
        :steps:
         0. Clean sssd cache
         1. Run getent group for posix group and using id check that user
            is member of posix group.
        :expectedresults:
         0. Cache is cleared.
         1. The posix group gid is present in id output.
        :teardown:
         Remove the created user, groups and revert resolution order.
        :customerscenario: True
        :bugzilla:
         https://bugzilla.redhat.com/show_bug.cgi?id=1945552
         https://bugzilla.redhat.com/show_bug.cgi?id=1937919
         https://bugzilla.redhat.com/show_bug.cgi?id=1945654
        """
        ad_domain = multihost.ad[0].domainname
        ipaserver = sssdTools(multihost.master[0])
        ipa_domain = ipaserver.get_domain_section_name()
        (username, _) = create_aduser_group
        posix_group = "posix_group_01"
        ext_group = "ext_group_01"
        # SETUP
        # Set the domain resolution order to AD first
        resorder_cmd = f'ipa config-mod --domain-resolution-order=' \
                       f'{ad_domain}:{ipa_domain}'
        multihost.master[0].run_command(resorder_cmd, raiseonerr=False)

        # Create posix group
        pgroup_cmd = f'ipa group-add {posix_group}'
        multihost.master[0].run_command(pgroup_cmd, raiseonerr=False)

        # Create and external group
        ext_group_cmd = f'ipa group-add --external {ext_group}'
        multihost.master[0].run_command(ext_group_cmd, raiseonerr=False)

        # Set membership of external group in posix group
        member_cmd = f'ipa -n group-add-member {posix_group} --groups=' \
                     f'{ext_group}'
        multihost.master[0].run_command(member_cmd, raiseonerr=False)

        # Set AD user membership in external group
        usr_mbr_cmd = f"ipa -n group-add-member {ext_group} --external" \
                      f" '{username}@{ad_domain}'"
        multihost.master[0].run_command(usr_mbr_cmd, raiseonerr=False)

        # TEST
        # Get posix group id
        grp_show_cmd = f"ipa group-show {posix_group}"
        cmd = multihost.master[0].run_command(grp_show_cmd, raiseonerr=False)
        gid_regex = re.compile(r"GID: (\d+)")
        posix_group_id = gid_regex.search(cmd.stdout_text).group(1)

        # Check that external group is member of posix group
        grp_show_cmd = f"ipa group-show {ext_group}"
        cmd = multihost.master[0].run_command(grp_show_cmd, raiseonerr=False)
        assert posix_group in cmd.stdout_text, \
            "The external group is not a member of posix group!"

        # A bit of wait so the user is propagated
        time.sleep(60)

        # The reproduction rate is not 100%, I had reliably 2+
        # fails in 5 rounds.
        for _ in range(5):
            # Clean caches on SSSD so we don't have to wait for cache timeouts
            # The reproduction works better on sssd on ipa master
            sssd_client = sssdTools(multihost.master[0])
            sssd_client.clear_sssd_cache()

            # Search the posix group using getent to trigger the condition with
            # negative cache
            getent_cmd = f"getent group {posix_group_id}"
            multihost.master[0].run_command(getent_cmd, raiseonerr=False)

            # Check that posix group is listed in id
            id_cmd = f"id {username}@{ad_domain}"
            cmd = multihost.master[0].run_command(id_cmd, raiseonerr=False)
            # Check if id worked
            assert cmd.returncode == 0,\
                'Could not find the user, something wrong with setup!'
            # Check if the posix group was found for the user.
            assert posix_group_id in cmd.stdout_text,\
                "The user is not a member of posix group!"

        # TEARDOWN
        # Remove user from external group
        usr_mbr_del_cmd = f"ipa -n group-remove-member {ext_group} " \
                          f"--external '{username}@{ad_domain}'"
        multihost.master[0].run_command(usr_mbr_del_cmd, raiseonerr=False)

        # Remove group membership
        grp_del_mbr_cmd = f'ipa -n group-remove-member {posix_group}' \
                          f' --groups={ext_group}'
        multihost.master[0].run_command(grp_del_mbr_cmd, raiseonerr=False)

        # Remove external group
        ext_grp_del_cmd = f'ipa group-del {ext_group}'
        multihost.master[0].run_command(ext_grp_del_cmd, raiseonerr=False)

        # Remove posix group
        px_grp_del_cmd = f'ipa group-del {posix_group}'
        multihost.master[0].run_command(px_grp_del_cmd, raiseonerr=False)

        # Reset the domain resolution order
        rev_resorder_cmd = f'ipa config-mod --domain-resolution-order=' \
                           f'{ipa_domain}:{ad_domain}'
        multihost.master[0].run_command(rev_resorder_cmd, raiseonerr=False)

    def test_nss_get_by_name_with_private_group(self, multihost):
        """
        :title:
         SSSD fails nss_getby_name for IPA user with SID if the user has
         a private group
        :id: 45dce6b9-0d47-4b9f-9532-4da8178e5334
        :setup:
         1. Configure trust between IPA server and AD.
         2. Configure client machine with SSSD integrated to IPA.
         3. Create an user with a private group
        :steps:
         1. Call function getsidbyname from pysss_nss_idmap for admin.
         2. Call function getsidbyname from pysss_nss_idmap for then user.
        :expectedresults:
         1. The admin SID is returned.
         2. The user SID is returned.
        :teardown:
         Remove the created user.
        :bugzilla:
         https://bugzilla.redhat.com/show_bug.cgi?id=1837090
        """
        # Create an user with a private group
        username = 'some-user'
        multihost.master[0].run_command(
            f'ipa user-add {username} --first=Some --last=User',
            raiseonerr=False
        )

        # Confirm that the user exists
        cmd = multihost.master[0].run_command(
            f'id  {username}',
            raiseonerr=False
        )
        # First check for admin user to make sure that the setup is correct
        check_admin_cmd = '''python3 -c "import pysss_nss_idmap; import '''\
            '''sys; result=pysss_nss_idmap.getsidbyname('admin');'''\
            '''print(result); result or sys.exit(2)"'''
        cmd_adm = multihost.master[0].run_command(check_admin_cmd,
                                                  raiseonerr=False)

        # Now check for the user with the private group
        check_user_cmd = '''python3 -c "import pysss_nss_idmap; import sys;'''\
            '''result=pysss_nss_idmap.getsidbyname('%s');print(result); '''\
            '''result or sys.exit(2)"''' % username
        cmd_usr = multihost.master[0].run_command(check_user_cmd,
                                                  raiseonerr=False)

        # Remove the user afterwards
        user_del_cmd = f'ipa user-del {username}'
        multihost.master[0].run_command(user_del_cmd, raiseonerr=False)

        # Evaluate results after cleanup is done
        assert cmd.returncode == 0, 'Could not find the user!'
        assert cmd_adm.returncode == 0, 'Something wrong with setup!'
        assert cmd_usr.returncode == 0, \
            f"pysss_nss_idmap.getsidbyname for {username} failed"

    @staticmethod
    def test_idview_override_group_fails(multihost, create_aduser_group):
        """
        :title: IPA clients fail to resolve override group names in custom view
        :id: 7a0dc871-fdad-4c07-9d07-a092baa83178
        :customerscenario: true
        :bugzilla:
          https://bugzilla.redhat.com/show_bug.cgi?id=2004406
          https://bugzilla.redhat.com/show_bug.cgi?id=2031729
        :description: Overriding both user and group names and ids in
          an idview for user and group from AD results in error in sssd
          when running id command.
        :setup:
          1. Create user and group (group1) on AD.
          2. Make AD user member of group1.
          3. Create additional group (group2) on AD.
        :steps:
          1. ID views to override AD groupname and gid of group1.
          2. ID views to override AD groupname and gid of group2.
          3. ID view to override AD username, uid and gid (to gid of group2).
          4. Run an "id" command for the override user.
        :expectedresults:
          1. View with an override is created.
          2. View with an override is created.
          3. User override is added to the view.
          4. Id command succeeds, group override is visible, all groups are
             properly resolved.
        """
        (aduser, adgroup) = create_aduser_group
        run_id_int = random.randint(9999, 999999)
        adgroup2 = f"group2_{run_id_int}"
        ado = ADOperations(multihost.ad[0])
        ado.create_ad_unix_group(adgroup2)
        domain = multihost.ad[0].domainname

        ipa_client = sssdTools(multihost.client[0])
        ipa_client.clear_sssd_cache()

        view = f'prygl_trust_view_{run_id_int}'
        create_view = f'ipa idview-add {view}'
        multihost.master[0].run_command(create_view, raiseonerr=False)

        create_grp_override = f'ipa idoverridegroup-add "{view}" ' \
            f'{adgroup}@{domain} --group-name ' \
            f'"borci{run_id_int}" --gid={run_id_int+1}'
        multihost.master[0].run_command(create_grp_override, raiseonerr=False)

        create_grp2_override = f'ipa idoverridegroup-add "{view}" ' \
            f'{adgroup2}@{domain} --group-name ' \
            f'"magori{run_id_int}" --gid={run_id_int+2}'
        multihost.master[0].run_command(create_grp2_override, raiseonerr=False)

        create_user_override = f'ipa idoverrideuser-add "{view}" ' \
            f'{aduser}@{domain} --login ferko{run_id_int} ' \
            f'--uid=50001 --gidnumber={run_id_int+2}'
        multihost.master[0].run_command(create_user_override, raiseonerr=False)

        # Apply the view on client
        multihost.master[0].run_command(
            f"ipa idview-apply '{view}' --hosts="
            f"{multihost.client[0].sys_hostname}", raiseonerr=False)

        ipa_client.clear_sssd_cache()
        time.sleep(5)
        cmd = multihost.client[0].run_command(
            f'id ferko{run_id_int}@{domain}', raiseonerr=False)

        # TEARDOWN
        ado.delete_ad_user_group(adgroup2)
        multihost.master[0].run_command(
            f'ipa idview-del {view}', raiseonerr=False)

        # Test result Evaluation
        assert cmd.returncode == 0, f"User {aduser} was not found."
        assert f"borci{run_id_int}@{domain}" in cmd.stdout_text,\
            f"Group 1 {adgroup} name was not overridden/resolved."
        assert f"magori{run_id_int}@{domain}" in cmd.stdout_text,\
            f"Group 2 {adgroup2} name was not overridden/resolved."
        assert f"{run_id_int+1}" in cmd.stdout_text,\
            "Group 1 id was not overridden."
        assert f"{run_id_int+2}" in cmd.stdout_text,\
            "Group 2 id was not overridden."
        assert f"domain users@{domain}" in cmd.stdout_text, \
            "Group domain users is missing."
