import gzip
import tarfile
import psycopg2
import argparse
import logging
from cStringIO import StringIO

BLOCKSIZE = 1000  # restore blocks of 1,000 lines each

log = logging.getLogger()


def safe_restore(curs, gzfile, tablename, blocksize=BLOCKSIZE):
    """
    Restore a gzipped postgres table into the database, by skipping
    the ids which are already taken. Assume that the first field of
    the table is an integer id and that gzfile.name has the form
    '/some/path/tablename.csv.gz'
    The file is restored in blocks to avoid memory issues.

    :param curs: a psycopg2 cursor
    :param gzfile: a file object
    :param str tablename: full table name
    :param int blocksize: number of lines in a block
    """
    curs.execute('select id from %s' % tablename)
    ids = set(r[0] for r in curs.fetchall())
    s = StringIO()
    imported = 0
    try:
        for i, line in enumerate(gzfile, 1):
            id_ = int(line.split('\t', 1)[0])
            if id_ not in ids:
                s.write(line)
                imported += 1
            if i % BLOCKSIZE == 0:
                s.seek(0)
                curs.copy_from(s, tablename)
                s.close()
                s = StringIO()
    finally:
        s.seek(0)
        curs.copy_from(s, tablename)
        s.close()
    return imported


def hazard_restore(conn, tar):
    """
    Import a tar file generated by the HazardDumper.

    :param conn: the psycopg2 connection to the db
    :param tar: the pathname to the tarfile
    """
    curs = conn.cursor()
    tf = tarfile.open(tar)
    try:
        for line in tf.extractfile('hazard_calculation/FILENAMES.txt'):
            fname = line.rstrip()
            tname = fname[:-7]  # strip .csv.gz
            fileobj = tf.extractfile('hazard_calculation/%s' % fname)
            with gzip.GzipFile(fname, fileobj=fileobj) as f:
                log.info('Importing %s...', fname)
                imported = safe_restore(curs, f, tname)
                log.info('Imported %d new rows', imported)
    except:
        conn.rollback()
        raise
    else:
        conn.commit()
    log.info('Restored %s', tar)


def hazard_restore_remote(tar, host, dbname, user, password, port):
    conn = psycopg2.connect(
        host=host, dbname=dbname, user=user, password=password, port=port)
    hazard_restore(conn, tar)


def hazard_restore_local(tar):
    """
    Use the current django settings to restore hazard
    """
    from django.conf import settings
    default_cfg = settings.DATABASES['default']

    hazard_restore_remote(
        tar,
        default_cfg['HOST'],
        default_cfg['NAME'],
        default_cfg['USER'],
        default_cfg['PASSWORD'],
        # avoid passing an empty string to psycopg
        default_cfg['PORT'] or None)


if __name__ == '__main__':
    # not using the predefined Django connections here since
    # we may want to restore the tarfile into a remote db
    p = argparse.ArgumentParser()
    p.add_argument('tarfile')
    p.add_argument('host', nargs='?', default='localhost')
    p.add_argument('dbname', nargs='?', default='openquake')
    p.add_argument('user', nargs='?', default='oq_admin')
    p.add_argument('password', nargs='?', default='')
    p.add_argument('port', nargs='?', default='5432')
    arg = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    hazard_restore_remote(arg.tarfile, arg.host, arg.dbname,
                               arg.user, arg.password, arg.port)
