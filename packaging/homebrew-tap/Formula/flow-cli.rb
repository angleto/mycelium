class FlowCli < Formula
  include Language::Python::Virtualenv

  desc "Keyboard-first CLI for Flow (tasks, notes, time, calendar)"
  homepage "https://github.com/angleto/flow"
  # Update url + sha256 on each release. ``bin/release-formula`` in
  # this repo prints the block ready to paste.
  url "https://github.com/angleto/flow/archive/refs/tags/cli-v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_SHA256"
  license "AGPL-3.0-or-later"
  head "https://github.com/angleto/flow.git", branch: "v1.2"

  depends_on "python@3.12"

  # Optional runtime dep used by ``flow note voice``. Homebrew formulae
  # cannot declare ``=> :optional`` for runtime since 2020; if the user
  # wants voice recording they ``brew install sox`` themselves. The CLI
  # prints a helpful hint when sox/ffmpeg is missing.

  conflicts_with "flow",
    because: "both install a `flow` binary. Use Meta's Flow type checker via " \
             "`brew install --force flow` if you really need both."

  # Python resources. Regenerate when bumping deps:
  #   brew install homebrew/cask/homebrew-pypi-poet  # or use:
  #   pipx run homebrew-pypi-poet -f flow-cli > /tmp/res.rb
  # then paste below.
  #
  # Pinned at the versions resolved by uv.lock for flow-cli 0.1.0.
  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-8.1.7.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/source/t/typer/typer-0.25.1.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-15.0.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/source/m/markdown-it-py/markdown-it-py-4.2.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/source/m/mdurl/mdurl-0.1.2.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/source/p/pygments/pygments-2.18.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.27.2.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "httpcore" do
    url "https://files.pythonhosted.org/packages/source/h/httpcore/httpcore-1.0.6.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "h11" do
    url "https://files.pythonhosted.org/packages/source/h/h11/h11-0.14.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/source/a/anyio/anyio-4.6.2.post1.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "sniffio" do
    url "https://files.pythonhosted.org/packages/source/s/sniffio/sniffio-1.3.1.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/source/i/idna/idna-3.10.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/source/c/certifi/certifi-2024.8.30.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "platformdirs" do
    url "https://files.pythonhosted.org/packages/source/p/platformdirs/platformdirs-4.3.6.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "tomli-w" do
    url "https://files.pythonhosted.org/packages/source/t/tomli-w/tomli_w-1.0.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/source/p/pydantic/pydantic-2.9.2.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "pydantic-core" do
    url "https://files.pythonhosted.org/packages/source/p/pydantic-core/pydantic_core-2.23.4.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/source/a/annotated-types/annotated_types-0.7.0.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "shellingham" do
    url "https://files.pythonhosted.org/packages/source/s/shellingham/shellingham-1.5.4.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/source/t/typing-extensions/typing_extensions-4.12.2.tar.gz"
    sha256 "REPLACE_WITH_PYPI_SHA256"
  end

  def install
    # The monorepo nests the CLI under cli/. ``virtualenv_install_with_resources``
    # does not have a ``subdir:`` option directly, so we cd in.
    venv = virtualenv_create(libexec, "python3.12")
    resources.each { |r| venv.pip_install r }
    cd "cli" do
      venv.pip_install_and_link buildpath/"cli"
    end
  end

  def caveats
    <<~EOS
      Optional: install `sox` (or `ffmpeg`) to record voice notes:
        brew install sox

      First run:
        flow auth login --base-url https://flow.leto.blue

      Neovim integration:
        https://github.com/angleto/flow/tree/main/nvim/flow.nvim
    EOS
  end

  test do
    assert_match "flow-cli #{version}", shell_output("#{bin}/flow --version")
    # ``flow auth status`` exits 1 without a credential; that itself
    # exercises argument parsing + error rendering.
    output = shell_output("#{bin}/flow auth status 2>&1", 1)
    assert_match "not logged in", output
  end
end
