"""Choosing on the phone which pictures are in the slideshow."""
import os
import time

from PIL import Image

from frame import library
from frame.slideshow import NativeSlideshow


def photos(tmp_path, names, orient='horizontal'):
    d = tmp_path / orient
    d.mkdir(exist_ok=True)
    for i, n in enumerate(names):
        p = d / n
        Image.new('RGB', (64, 40), (i * 40, 10, 10)).save(p, 'JPEG')
        os.utime(p, (1000 + i, 1000 + i))          # later name = newer
    return d


def test_everything_is_shown_until_something_is_unticked(tmp_path):
    d = photos(tmp_path, ['a.jpg', 'b.jpg', 'c.jpg'])
    paths = [str(d / n) for n in ('a.jpg', 'b.jpg', 'c.jpg')]
    assert library.visible(paths, tmp_path) == paths
    library.set_hidden(tmp_path, ['b.jpg'], True)
    assert library.visible(paths, tmp_path) == [paths[0], paths[2]]
    assert library.is_hidden(tmp_path, paths[1])
    library.set_hidden(tmp_path, ['b.jpg'], False)
    assert library.visible(paths, tmp_path) == paths


def test_unticking_everything_still_shows_pictures(tmp_path):
    d = photos(tmp_path, ['a.jpg', 'b.jpg'])
    paths = [str(d / 'a.jpg'), str(d / 'b.jpg')]
    library.set_hidden(tmp_path, ['a.jpg', 'b.jpg'], True)
    assert library.visible(paths, tmp_path) == paths


def test_newest_pictures_come_first(tmp_path):
    d = photos(tmp_path, ['a.jpg', 'b.jpg', 'c.jpg'])
    os.utime(d / 'a.jpg', None)                     # a arrived just now
    library.set_hidden(tmp_path, ['c.jpg'], True)
    items = library.listing(tmp_path, 'horizontal')
    assert [i['name'] for i in items] == ['a.jpg', 'c.jpg', 'b.jpg']
    assert [i['new'] for i in items] == [True, False, False]
    assert [i['hidden'] for i in items] == [False, True, False]


def test_only_real_picture_names_are_stored(tmp_path):
    photos(tmp_path, ['a.jpg'])
    got = library.set_hidden(
        tmp_path, ['../config.yaml', '/etc/passwd', '.x.jpg', 'gone.jpg', 7, 'a.jpg'], True)
    assert got == {'a.jpg'}
    (tmp_path / 'horizontal' / 'a.jpg').unlink()
    assert library.set_hidden(tmp_path, [], True) == set()   # deleted picture drops out


def test_a_broken_list_shows_everything(tmp_path):
    d = photos(tmp_path, ['a.jpg'])
    (tmp_path / library.HIDDEN_FILE).write_text('{not json')
    assert library.visible([str(d / 'a.jpg')], tmp_path) == [str(d / 'a.jpg')]


def test_thumbnail_is_small_cached_and_safe(tmp_path):
    d = photos(tmp_path, ['a.jpg'])
    Image.new('RGB', (1920, 1200), (5, 90, 5)).save(d / 'a.jpg', 'JPEG')
    t = library.thumb(tmp_path, 'horizontal', 'a.jpg')
    with Image.open(t) as im:
        assert max(im.size) <= library.THUMB_SIZE
    made = os.path.getmtime(t)
    assert library.thumb(tmp_path, 'horizontal', 'a.jpg') == t
    assert os.path.getmtime(t) == made              # not rendered twice
    assert library.thumb(tmp_path, 'horizontal', '../hidden.json') is None
    assert library.thumb(tmp_path, '..', 'a.jpg') is None
    assert library.thumb(tmp_path, 'horizontal', 'nope.jpg') is None
    (d / 'a.jpg').unlink()
    library.sweep_thumbs(tmp_path)
    assert not os.path.exists(t)


def test_slideshow_leaves_out_unticked_pictures(tmp_path):
    photos(tmp_path, ['a.jpg', 'b.jpg', 'c.jpg'])
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('setup_complete: true\nframe: {orientation: horizontal}\n')
    s = NativeSlideshow(str(tmp_path), str(cfg))
    assert len(s._playlist()) == 3
    library.set_hidden(tmp_path, ['b.jpg'], True)
    time.sleep(0.01)
    names = sorted(os.path.basename(p) for p in s._playlist())
    assert names == ['a.jpg', 'c.jpg'] and s._all_hidden is False
    items = [str(tmp_path / 'horizontal' / n) for n in ('a.jpg', 'b.jpg', 'c.jpg')]
    assert s._next_photo(items, 0) == items[2]      # b is skipped for pre-render
    library.set_hidden(tmp_path, ['a.jpg', 'c.jpg'], True)
    assert len(s._playlist()) == 3 and s._all_hidden is True
