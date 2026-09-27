"""CFFI symbol definitions for libiperf API."""

CDEF = r"""
typedef struct iperf_test iperf_test;

/* lifecycle */
iperf_test *iperf_new_test(void);
int iperf_defaults(iperf_test *t);
void iperf_free_test(iperf_test *t);

/* params */
void iperf_set_test_role(iperf_test *t, char role); /* 'c' or 's' */
void iperf_set_test_server_hostname(iperf_test *t, char *server_host);
void iperf_set_test_server_port(iperf_test *t, int port);
void iperf_set_test_duration(iperf_test *t, int seconds);
void iperf_set_test_num_streams(iperf_test *t, int n);
void iperf_set_test_blksize(iperf_test *t, int bytes);
void iperf_set_test_tos(iperf_test *t, int tos);
void iperf_set_test_omit(iperf_test *t, int seconds);
void iperf_set_test_reverse(iperf_test *t, int on);
int set_protocol(iperf_test *t, int protocol_id);
int iperf_get_test_protocol_id(iperf_test *t);
void iperf_set_test_bind_address(iperf_test *t, const char *bind_address);
char *iperf_get_test_bind_address(iperf_test *t);

/* optional / feature gated (may not exist on older libs) */
void iperf_set_test_json_output(iperf_test *t, int on);
void iperf_set_test_json_stream(iperf_test *t, int on);
void iperf_set_test_bidirectional(iperf_test *t, int on);
void iperf_set_test_json_stream_full_output(iperf_test *t, int on);
void iperf_set_test_rate(iperf_test *t, unsigned long long rate);
void iperf_set_test_json_callback(
    iperf_test *t,
    void (*callback)(iperf_test *, char *)
);

/* Public configuration API shared by supported native versions. */
int iperf_parse_arguments(iperf_test *t, int argc, char **argv);
int iperf_get_test_duration(iperf_test *t);
int iperf_get_test_omit(iperf_test *t);
char iperf_get_test_role(iperf_test *t);
int iperf_get_test_reverse(iperf_test *t);
int iperf_get_test_bidirectional(iperf_test *t);
int iperf_get_test_num_streams(iperf_test *t);
int iperf_get_test_blksize(iperf_test *t);
int iperf_get_test_tos(iperf_test *t);
unsigned long long iperf_get_test_rate(iperf_test *t);
int iperf_get_test_server_port(iperf_test *t);
char *iperf_get_test_server_hostname(iperf_test *t);
void iperf_set_test_bind_dev(iperf_test *t, const char *device);
char *iperf_get_test_bind_dev(iperf_test *t);
void iperf_set_test_bind_port(iperf_test *t, int port);
int iperf_get_test_bind_port(iperf_test *t);
void iperf_set_test_socket_bufsize(iperf_test *t, int size);
int iperf_get_test_socket_bufsize(iperf_test *t);
void iperf_set_test_congestion_control(iperf_test *t, char *algorithm);
char *iperf_get_test_congestion_control(iperf_test *t);
void iperf_set_test_no_delay(iperf_test *t, int enabled);
int iperf_get_test_no_delay(iperf_test *t);
void iperf_set_test_mss(iperf_test *t, int size);
int iperf_get_test_mss(iperf_test *t);
void iperf_set_test_connect_timeout(iperf_test *t, int milliseconds);
int iperf_get_test_connect_timeout(iperf_test *t);
void iperf_set_test_bytes(iperf_test *t, unsigned long long count);
unsigned long long iperf_get_test_bytes(iperf_test *t);
void iperf_set_test_blocks(iperf_test *t, unsigned long long count);
unsigned long long iperf_get_test_blocks(iperf_test *t);
void iperf_set_test_burst(iperf_test *t, int count);
int iperf_get_test_burst(iperf_test *t);
double iperf_get_test_reporter_interval(iperf_test *t);
double iperf_get_test_stats_interval(iperf_test *t);
void iperf_set_test_pacing_timer(iperf_test *t, int microseconds);
int iperf_get_test_pacing_timer(iperf_test *t);
int iperf_has_zerocopy(void);
void iperf_set_test_zerocopy(iperf_test *t, int enabled);
int iperf_get_test_zerocopy(iperf_test *t);
void iperf_set_test_udp_counters_64bit(iperf_test *t, int enabled);
int iperf_get_test_udp_counters_64bit(iperf_test *t);
void iperf_set_dont_fragment(iperf_test *t, int enabled);
int iperf_get_dont_fragment(iperf_test *t);
void iperf_set_test_repeating_payload(iperf_test *t, int enabled);
int iperf_get_test_repeating_payload(iperf_test *t);
void iperf_set_test_get_server_output(iperf_test *t, int enabled);
int iperf_get_test_get_server_output(iperf_test *t);
void iperf_set_test_extra_data(iperf_test *t, const char *data);
char *iperf_get_test_extra_data(iperf_test *t);
void iperf_set_test_one_off(iperf_test *t, int enabled);
int iperf_get_test_one_off(iperf_test *t);
int iperf_get_test_json_output(iperf_test *t);
int iperf_get_test_json_stream(iperf_test *t);
void iperf_set_test_client_username(iperf_test *t, const char *username);
void iperf_set_test_client_password(iperf_test *t, const char *password);
void iperf_set_test_client_rsa_pubkey(iperf_test *t, const char *key_base64);
void iperf_set_test_server_rsa_privkey(iperf_test *t, const char *key_base64);
void iperf_set_test_server_authorized_users(iperf_test *t, const char *path);
void iperf_set_test_server_skew_threshold(iperf_test *t, int seconds);

/* run/reset */
int iperf_run_client(iperf_test *t);
int iperf_run_server(iperf_test *t);
void iperf_reset_test(iperf_test *t);

/* output / errors */
char *iperf_get_test_json_output_string(iperf_test *t);
char *iperf_get_iperf_version(void);
extern int i_errno;
char *iperf_strerror(int);
"""
