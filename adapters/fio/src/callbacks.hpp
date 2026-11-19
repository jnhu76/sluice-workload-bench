// Callbacks shared across engine translation units (ADAPTER-ARCHITECTURE §4).
#pragma once

#include "fio_cxx.hpp"

namespace fio_sluice {

int sluice_init(struct thread_data* td);
void sluice_cleanup(struct thread_data* td);

int sluice_open_file(struct thread_data* td, struct fio_file* f);
int sluice_close_file(struct thread_data* td, struct fio_file* f);
int sluice_get_file_size(struct thread_data* td, struct fio_file* f);

enum fio_q_status sluice_queue(struct thread_data* td, struct io_u* io_u);
int sluice_commit(struct thread_data* td);

int sluice_getevents(struct thread_data* td, unsigned int min, unsigned int max,
                     const struct timespec* t);
struct io_u* sluice_event(struct thread_data* td, int i);

} // namespace fio_sluice
