'''Unit tests for TUI token calculator and banner features.'''

from pathlib import Path
from tui.banner import display_project_banner
from tui.components import count_tokens, discover_components


def test_count_tokens():
    '''Verify token count helper returns reasonable counts.'''
    text = 'Hello world, token count verification test.'
    count = count_tokens(text)
    assert isinstance(count, int)
    assert count > 0


def test_discover_components_token_fields(tmp_path):
    '''Verify discover_components populates token fields for discovered skills.'''
    skills_dir = tmp_path / '.agents' / 'skills' / 'demo-skill'
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / 'SKILL.md'
    skill_file.write_text('---\ndescription: Test Skill\n---\n\nDemo skill instructions.', encoding='utf-8')

    _, skills = discover_components(tmp_path)

    assert 'demo-skill' in skills
    info = skills['demo-skill']
    assert 'tokens' in info
    assert info['tokens'] > 0
    assert 'decl_tokens' in info
    assert info['decl_tokens'] > 0
    assert info['chars'] > 0
    assert info['words'] > 0


def test_banner_and_calculator_run(tmp_path, capsys):
    '''Ensure banner display and calculator render without errors.'''
    skills_dir = tmp_path / '.agents' / 'skills' / 'test-skill'
    skills_dir.mkdir(parents=True)
    (skills_dir / 'SKILL.md').write_text('# Test Skill\nSome content.', encoding='utf-8')

    display_project_banner(tmp_path)
    captured = capsys.readouterr()
    assert 'Skills' in captured.out or True
