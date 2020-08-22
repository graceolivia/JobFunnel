"""Filters that are used in jobfunnel's filter() method or as intermediate
filters to reduce un-necessesary scraping.
FIXME: we should have a Enum(Filter) for all job filters to allow configuration
and generic log messages.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
import json
import os

import nltk
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from jobfunnel.backend import Job
from jobfunnel.backend.tools import update_job_if_newer, get_logger
from jobfunnel.resources import (
    DEFAULT_MAX_TFIDF_SIMILARITY, MIN_JOBS_TO_PERFORM_SIMILARITY_SEARCH
)


T_NOW = datetime.now()


def job_is_old(job: Job, number_of_days: int) -> bool:
    """Identify if a job is older than number_of_days from today
    TODO: move this into Job.job_is_old()
    NOTE: modifies job_dict in-place

        Args:
            job_dict: today's job scrape dict
            number_of_days: how many days old a job can be

        Returns:
            True if it's older than number of days
            False if it's fresh enough to keep
    """
    assert number_of_days > 0
    # Calculate the oldest date a job can be
    # NOTE: we may want to just set job.status = JobStatus.OLD
    return job.post_date < (T_NOW - timedelta(days=number_of_days))


def tfidf_filter(cur_dict: Dict[str, dict],
                 prev_dict: Optional[Dict[str, dict]] = None,
                 max_similarity: float = DEFAULT_MAX_TFIDF_SIMILARITY,
                 duplicate_jobs_file: Optional[str] = None,
                 log_level: int = logging.INFO,
                 log_file: str = None,
                 ) -> List[Job]:
    """Fit a tfidf vectorizer to a corpus of Job.DESCRIPTIONs and identify
    duplicate jobs by cosine-similarity.

    NOTE: This will update jobs in cur_dict if the content match has a newer
        post_date.
    NOTE/WARNING: if you are running this method, you should have already
        removed any duplicates by key_id
    FIXME: we should make max_similarity configurable in SearchConfig
    FIXME: this should be integrated into jobfunnel.filter with other filters
    FIXME: fix logger arg-passing once we get this in some kind of class
    NOTE: this only uses job descriptions to do the content matching.
    NOTE: it is recommended that you have at least around 25 Jobs.
    TODO: have this raise an exception if there are too few words?
    FIXME: make this a class so we can call it many times on single queries.

    Args:
        cur_dict (Dict[str, dict]): dict of jobs containing potential duplicates
             (i.e jobs we just scraped)
        prev_dict (Optional[Dict[str, dict]], optional): the existing jobs dict
            (i.e. master CSV contents). If None, we will remove duplicates
            from within the cur_dict only. Defaults to None.
        max_similarity (float, optional): threshold above which blurb similarity
            is considered a duplicate. Defaults to DEFAULT_MAX_TFIDF_SIMILARITY.
        duplicate_jobs_file (str, optional): location to save duplicates that
            we identify via content matching. Defaults to None.
        ...

    Raises:
        ValueError: cur_dict contains no job descriptions

    Returns:
        List[Job]: list of duplicate Jobs which were removed from cur_dict
    """
    logger = get_logger(
        tfidf_filter.__name__,
        log_level,
        log_file,
        f"[%(asctime)s] [%(levelname)s] {tfidf_filter.__name__}: %(message)s"
    )

    # Retrieve stopwords if not already downloaded
    # TODO: we should use this to make jobs attrs tokenizable as a property.
    # TODO: make the vectorizer persistant.
    try:
        stopwords = nltk.corpus.stopwords.words('english')
    except LookupError:
        nltk.download('stopwords', quiet=True)
        stopwords = nltk.corpus.stopwords.words('english')

    # init vectorizer NOTE: pretty fast call but we should do this once!
    vectorizer = TfidfVectorizer(
        strip_accents='unicode',
        lowercase=True,
        analyzer='word',
        stop_words=stopwords,
    )

    # Load known duplicate keys from JSON if we have it
    # NOTE: this allows us to do smaller TFIDF comparisons because we ensure
    # that we are skipping previously-detected job duplicates (by id)
    existing_duplicate_keys = {}  # type: Set[str]
    existing_duplicate_jobs_dict = {}  # type: Dict[str, str]
    if duplicate_jobs_file and os.path.isfile(duplicate_jobs_file):
        existing_duplicate_jobs_dict = json.load(
            open(duplicate_jobs_file, 'r')
        )
        existing_duplicate_keys = existing_duplicate_jobs_dict.keys()

    def __dict_to_ids_and_words(jobs_dict: Dict[str, Job]
                                ) -> Tuple[List[str], List[str]]:
        """Get query words and ids as lists + prefilter
        NOTE: this is just a convenience method since we do this 2x
        """
        ids = []  # type: List[str]
        words = []  # type: List[str]
        filt_job_dict = {}  # type: Dict[str, Job]
        for job in cur_dict.values():
            if job.key_id in existing_duplicate_keys:
                logger.debug(
                    f"Removing {job.key_id} from scrape result, existing "
                    "duplicate."
                )
            elif not len(job.description):
                logger.debug(
                    f"Removing {job.key_id} from scrape result, empty "
                    "description."
                )
            else:
                ids.append(job.key_id)
                words.append(job.description)
                # NOTE: We want to leave changing cur_dict in place till the end
                # or we will break usage of update_job_if_newer()
                filt_job_dict[job.key_id] = job

        # TODO: assert on length of contents of the lists as well
        if not words:
            raise ValueError(
                "No data to fit, are your job descriptions all empty?"
            )
        return ids, words, filt_job_dict

    query_ids, query_words, filt_cur_dict = __dict_to_ids_and_words(cur_dict)
    reference_ids, reference_words, filt_prev_dict = __dict_to_ids_and_words(
        prev_dict
    )

    # Provide a warning if we have few words.
    corpus = query_words + reference_words
    if len(corpus) < MIN_JOBS_TO_PERFORM_SIMILARITY_SEARCH:
        logger.warning(
            "It is not recommended to use this filter with less than "
            f"{MIN_JOBS_TO_PERFORM_SIMILARITY_SEARCH} words"
        )

    # Fit vectorizer to entire corpus
    vectorizer.fit(corpus)

    # Calculate cosine similarity between reference and current blurbs
    # This is a list of the similarity between that query job and all the
    # TODO: impl. in a more efficient way since fit() does the transform already
    similarities_per_query = cosine_similarity(
        vectorizer.transform(query_words),
        vectorizer.transform(reference_words),
    )

    # Get duplicate job ids and pop them, updating cur_dict if they are newer
    duplicate_jobs_list = []  # type: List[Job]
    for query_similarities, query_id in zip(similarities_per_query, query_ids):

        # Identify the jobs in prev_dict that our query is a duplicate of
        # FIXME: handle if everything is highly similar!
        for similar_index in np.where(query_similarities >= max_similarity)[0]:
            update_job_if_newer(
                filt_prev_dict[reference_ids[similar_index]],
                filt_cur_dict[query_id],
            )
            duplicate_jobs_list.append(filt_cur_dict[query_id])

    if duplicate_jobs_list:

        # NOTE: multiple jobs can be a duplicate of the same job.
        duplicate_ids = {job.key_id for job in duplicate_jobs_list}

        # Remove duplicates from cur_dict + save to our duplicates file
        for key_id in duplicate_ids:
            cur_dict.pop(key_id)
            logger.debug(
                f"Removed {key_id} from scraped data, TFIDF content match."
            )

        logger.info(
            f'Found and removed {len(duplicate_jobs_list)} '
            f're-posts/duplicate postings via TFIDF cosine similarity.'
        )

        if duplicate_jobs_file:
            # Write out a list of duplicates so that detections persist under
            # changing input data.
            existing_duplicate_jobs_dict.update(
                {dj.key_id: dj.as_json_entry for dj in duplicate_jobs_list}
            )
            with open(duplicate_jobs_file, 'w', encoding='utf8') as outfile:
                # NOTE: we use indent=4 so that it stays human-readable.
                outfile.write(
                    json.dumps(
                        existing_duplicate_jobs_dict,
                        indent=4,
                        sort_keys=True,
                        separators=(',', ': '),
                        ensure_ascii=False,
                    )
                )
        else:
            logger.warning(
                "Duplicates will not be saved, no duplicates list file set. "
                "Saving to a duplicates file will ensure that these persist."
            )

    # returns a list of duplicate Jobs
    return duplicate_jobs_list
