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

/* optional / feature gated (may not exist on older libs) */
void iperf_set_test_json_output(iperf_test *t, int on);
void iperf_set_test_json_stream(iperf_test *t, int on);
void iperf_set_test_bidirectional(iperf_test *t, int on);
void iperf_set_test_json_stream_full_output(iperf_test *t, int on);
void iperf_set_test_rate(iperf_test *t, unsigned long long rate);

/* run/reset */
int iperf_run_client(iperf_test *t);
int iperf_run_server(iperf_test *t);
void iperf_reset_test(iperf_test *t);

/* output / errors */
char *iperf_get_test_json_output_string(iperf_test *t);
extern int i_errno;
char *iperf_strerror(int);
"""
