# shellcheck shell=sh
# Installed as /etc/profile.d/nako.sh. Debian's /etc/profile resets PATH for
# every login shell (`bash -l`, `docker exec <c> bash -l`, ssh, the terminal
# tool's background `bash -lic`), dropping the image's ENV PATH dirs, so bare
# `nako` exits 127 there. Put them back in the ENV PATH order; dirs a login
# PATH already lists keep their position.
for __nako_p in /opt/data/.local/bin /opt/nako/.venv/bin /opt/nako/bin; do
    case ":$PATH:" in
        *":$__nako_p:"*) ;;
        *) PATH="$__nako_p:$PATH" ;;
    esac
done
unset __nako_p
export PATH
