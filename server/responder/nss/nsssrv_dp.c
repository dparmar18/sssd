/*
   SSSD

   NSS Responder - Data Provider Interfaces

   Copyright (C) Simo Sorce <ssorce@redhat.com>	2008

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation; either version 3 of the License, or
   (at your option) any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include <sys/time.h>
#include <time.h>
#include "util/util.h"
#include "responder/common/responder_packet.h"
#include "responder/nss/nsssrv.h"
#include "providers/data_provider.h"
#include "sbus/sbus_client.h"
#include "providers/dp_sbus.h"

struct nss_dp_req {
    nss_dp_callback_t callback;
    void *callback_ctx;
    struct tevent_timer *te;
    DBusPendingCall *pending_reply;
};

static int nss_dp_req_destructor(void *ptr)
{
    struct nss_dp_req *req = talloc_get_type(ptr, struct nss_dp_req);

    if (req->pending_reply) {
        dbus_pending_call_cancel(req->pending_reply);
    }

    return 0;
}

static void nss_dp_send_acct_timeout(struct tevent_context *ev,
                                     struct tevent_timer *te,
                                     struct timeval t, void *data)
{
    struct nss_dp_req *ndp_req;
    dbus_uint16_t err_maj = DP_ERR_TIMEOUT;
    dbus_uint32_t err_min = EIO;
    const char *err_msg = "Request timed out";

    ndp_req = talloc_get_type(data, struct nss_dp_req);

    ndp_req->callback(err_maj, err_min, err_msg, ndp_req->callback_ctx);

    talloc_free(ndp_req);
}

static int nss_dp_get_reply(DBusPendingCall *pending,
                            dbus_uint16_t *err_maj,
                            dbus_uint32_t *err_min,
                            const char **err_msg);

static void nss_dp_send_acct_callback(DBusPendingCall *pending, void *ptr)
{
    struct nss_dp_req *ndp_req;
    dbus_uint16_t err_maj;
    dbus_uint32_t err_min;
    const char *err_msg;
    int ret;

    ndp_req = talloc_get_type(ptr, struct nss_dp_req);

    /* free timeout event and remove request destructor */
    talloc_free(ndp_req->te);
    talloc_set_destructor(ndp_req, NULL);

    ret = nss_dp_get_reply(pending, &err_maj, &err_min, &err_msg);
    if (ret != EOK) {
        err_maj = DP_ERR_FATAL;
        err_min = ret;
        err_msg = "Failed to get reply from Data Provider";
    }

    ndp_req->callback(err_maj, err_min, err_msg, ndp_req->callback_ctx);

    talloc_free(ndp_req);
}

int nss_dp_send_acct_req(struct resp_ctx *rctx, TALLOC_CTX *memctx,
                         nss_dp_callback_t callback, void *callback_ctx,
                         int timeout, const char *domain, int type,
                         const char *opt_name, uint32_t opt_id)
{
    struct nss_dp_req *ndp_req;
    DBusMessage *msg;
    DBusPendingCall *pending_reply;
    DBusConnection *conn;
    dbus_bool_t ret;
    uint32_t be_type;
    const char *attrs = "core";
    char *filter;
    struct timeval tv;

    /* either, or, not both */
    if (opt_name && opt_id) {
        return EINVAL;
    }

    if (!domain) {
        return EINVAL;
    }

    switch (type) {
    case NSS_DP_USER:
        be_type = BE_REQ_USER;
        break;
    case NSS_DP_GROUP:
        be_type = BE_REQ_GROUP;
        break;
    case NSS_DP_INITGROUPS:
        be_type = BE_REQ_INITGROUPS;
        break;
    default:
        return EINVAL;
    }

    if (opt_name) {
        filter = talloc_asprintf(memctx, "name=%s", opt_name);
    } else if (opt_id) {
        filter = talloc_asprintf(memctx, "idnumber=%u", opt_id);
    } else {
        filter = talloc_strdup(memctx, "name=*");
    }
    if (!filter) {
        return ENOMEM;
    }

    conn = sbus_get_connection(rctx->dp_ctx->scon_ctx);

    /* create the message */
    msg = dbus_message_new_method_call(NULL,
                                       DP_CLI_PATH,
                                       DP_CLI_INTERFACE,
                                       DP_SRV_METHOD_GETACCTINFO);
    if (msg == NULL) {
        DEBUG(0,("Out of memory?!\n"));
        return ENOMEM;
    }

    DEBUG(4, ("Sending request for [%s][%u][%s][%s]\n",
              domain, be_type, attrs, filter));

    ret = dbus_message_append_args(msg,
                                   DBUS_TYPE_STRING, &domain,
                                   DBUS_TYPE_UINT32, &be_type,
                                   DBUS_TYPE_STRING, &attrs,
                                   DBUS_TYPE_STRING, &filter,
                                   DBUS_TYPE_INVALID);
    if (!ret) {
        DEBUG(1,("Failed to build message\n"));
        return EIO;
    }

    ret = dbus_connection_send_with_reply(conn, msg, &pending_reply,
                                            600000 /* TODO: set timeout */);
    if (!ret) {
        /*
         * Critical Failure
         * We can't communicate on this connection
         * We'll drop it using the default destructor.
         */
        DEBUG(0, ("D-BUS send failed.\n"));
        dbus_message_unref(msg);
        return EIO;
    }

    ndp_req = talloc_zero(memctx, struct nss_dp_req);
    if (!ndp_req) {
        dbus_message_unref(msg);
        return ENOMEM;
    }
    ndp_req->callback = callback;
    ndp_req->callback_ctx = callback_ctx;

    /* set up destructor */
    ndp_req->pending_reply = pending_reply;
    talloc_set_destructor((TALLOC_CTX *)ndp_req, nss_dp_req_destructor);

    /* setup the timeout handler */
    gettimeofday(&tv, NULL);
    tv.tv_sec += timeout/1000;
    tv.tv_usec += (timeout%1000) * 1000;
    ndp_req->te = tevent_add_timer(rctx->ev, memctx, tv,
                                   nss_dp_send_acct_timeout, ndp_req);

    /* Set up the reply handler */
    dbus_pending_call_set_notify(pending_reply,
                                 nss_dp_send_acct_callback,
                                 ndp_req, NULL);
    dbus_message_unref(msg);

    return EOK;
}

static int nss_dp_get_reply(DBusPendingCall *pending,
                            dbus_uint16_t *err_maj,
                            dbus_uint32_t *err_min,
                            const char **err_msg)
{
    DBusMessage *reply;
    DBusError dbus_error;
    dbus_bool_t ret;
    int type;
    int err = EOK;

    dbus_error_init(&dbus_error);

    reply = dbus_pending_call_steal_reply(pending);
    if (!reply) {
        /* reply should never be null. This function shouldn't be called
         * until reply is valid or timeout has occurred. If reply is NULL
         * here, something is seriously wrong and we should bail out.
         */
        DEBUG(0, ("Severe error. A reply callback was called but no reply was received and no timeout occurred\n"));

        /* FIXME: Destroy this connection ? */
        err = EIO;
        goto done;
    }

    type = dbus_message_get_type(reply);
    switch (type) {
    case DBUS_MESSAGE_TYPE_METHOD_RETURN:
        ret = dbus_message_get_args(reply, &dbus_error,
                                    DBUS_TYPE_UINT16, err_maj,
                                    DBUS_TYPE_UINT32, err_min,
                                    DBUS_TYPE_STRING, err_msg,
                                    DBUS_TYPE_INVALID);
        if (!ret) {
            DEBUG(1,("Filed to parse message\n"));
            /* FIXME: Destroy this connection ? */
            if (dbus_error_is_set(&dbus_error)) dbus_error_free(&dbus_error);
            err = EIO;
            goto done;
        }

        DEBUG(4, ("Got reply (%u, %u, %s) from Data Provider\n",
                  (unsigned int)*err_maj, (unsigned int)*err_min, *err_msg));

        break;

    case DBUS_MESSAGE_TYPE_ERROR:
        DEBUG(0,("The Data Provider returned an error [%s]\n",
                 dbus_message_get_error_name(reply)));
        /* Falling through to default intentionally*/
    default:
        /*
         * Timeout or other error occurred or something
         * unexpected happened.
         * It doesn't matter which, because either way we
         * know that this connection isn't trustworthy.
         * We'll destroy it now.
         */

        /* FIXME: Destroy this connection ? */
        err = EIO;
    }

done:
    dbus_pending_call_unref(pending);
    dbus_message_unref(reply);

    return err;
}

static int nss_dp_identity(DBusMessage *message, struct sbus_conn_ctx *sconn)
{
    dbus_uint16_t version = DATA_PROVIDER_VERSION;
    dbus_uint16_t clitype = DP_CLI_FRONTEND;
    const char *cliname = "NSS";
    const char *nullname = "";
    DBusMessage *reply;
    dbus_bool_t ret;

    DEBUG(4,("Sending ID reply: (%d,%d,%s)\n",
             clitype, version, cliname));

    reply = dbus_message_new_method_return(message);
    if (!reply) return ENOMEM;

    ret = dbus_message_append_args(reply,
                                   DBUS_TYPE_UINT16, &clitype,
                                   DBUS_TYPE_UINT16, &version,
                                   DBUS_TYPE_STRING, &cliname,
                                   DBUS_TYPE_STRING, &nullname,
                                   DBUS_TYPE_INVALID);
    if (!ret) {
        dbus_message_unref(reply);
        return EIO;
    }

    /* send reply back */
    sbus_conn_send_reply(sconn, reply);
    dbus_message_unref(reply);

    return EOK;
}

static struct sbus_method nss_dp_methods[] = {
    { DP_CLI_METHOD_IDENTITY, nss_dp_identity },
    { NULL, NULL }
};

struct sbus_method *get_nss_dp_methods(void)
{
    return nss_dp_methods;
}
